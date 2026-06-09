// CamHeaderBridge (JP6 / GStreamer 1.20): consume the core's `application/x-cam-frame` shm endpoint
// and republish each frame as sensor_msgs/Image, with header.stamp from the per-frame hardware (PTP)
// timestamp carried in the 36-byte header. This is the legacy transport: shm drops caps/PTS/meta, so
// the core prepends a header. (JP7 uses the header-free unixfd transport -- see cam_unixfd_bridge.cpp.)
//
// Color on JP6: this component always publishes the mosaic labeled bayer_* (option A). When debayer is
// requested the launch file composes image_proc::DebayerNode into the same container (intra-process,
// zero-copy) -- so there is no in-process demosaic here anymore.
//
// The FrameHeader below MUST match cam_driver/transport.py (struct "<4sHHQQHHIBBH", 36 bytes, LE).
// Jetson (arm64) and x86 are both little-endian, so a packed struct maps the wire bytes directly.
#include <cstring>

#include <rclcpp_components/register_node_macro.hpp>

#include "cam_bridge_base.hpp"

namespace cam_ros2_bridge {
namespace {

constexpr char kMagic[4] = {'C', 'A', 'M', 'F'};
constexpr uint16_t kVersion = 1;

#pragma pack(push, 1)
struct FrameHeader {
  char     magic[4];     // "CAMF"
  uint16_t version;
  uint16_t header_len;   // offset to pixel data
  uint64_t timestamp_ns; // absolute capture time (ns); PTP epoch when locked
  uint64_t frame_id;
  uint16_t width;
  uint16_t height;
  uint32_t pixfmt;       // 1=GRAY8 2=GRAY16_LE 3=GRAY16_BE 4=I420 5=NV12 6=YUY2 7=RGB 8=BGR
                         // 9=NV24 10=YV12 11=UYVY 12=RGBA 13=BGRA 14=RGBx 15=BGRx
  uint8_t  ts_source;    // 0=ptp_chunk 1=camera 2=system 3=sof 4=rtp_ntp
  uint8_t  flags;
  uint16_t reserved;
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 36, "FrameHeader must match transport.py (36 bytes)");

struct PixInfo {
  const char* encoding;   // sensor_msgs encoding published (rgb8 for the converted YUV formats)
  int bytes_per_px;
  bool big_endian;
  Yuv yuv;                // != NONE -> convert the source plane to rgb8 (no YUV sensor_msgs encoding)
};

bool pixfmt_info(uint32_t code, PixInfo& out) {
  switch (code) {   // mirrors cam_driver/transport.py _CODE_TO_GST
    case 1: out = {"mono8", 1, false, Yuv::NONE}; return true;
    case 2: out = {"mono16", 2, false, Yuv::NONE}; return true;
    case 3: out = {"mono16", 2, true,  Yuv::NONE}; return true;
    case 4: out = {"rgb8", 3, false, Yuv::I420}; return true;   // I420 -> rgb8 (decoded color/RTSP)
    case 5: out = {"rgb8", 3, false, Yuv::NV12}; return true;   // NV12 -> rgb8
    case 6: out = {"rgb8", 3, false, Yuv::YUY2}; return true;   // YUY2 -> rgb8
    case 7: out = {"rgb8", 3, false, Yuv::NONE}; return true;   // RGB -> rgb8 (direct)
    case 8: out = {"bgr8", 3, false, Yuv::NONE}; return true;   // BGR -> bgr8 (direct)
    case 9: out = {"rgb8", 3, false, Yuv::NV24}; return true;   // NV24 (4:4:4) -> rgb8
    case 10: out = {"rgb8", 3, false, Yuv::YV12}; return true;  // YV12 (I420, V/U swapped) -> rgb8
    case 11: out = {"rgb8", 3, false, Yuv::UYVY}; return true;  // UYVY (4:2:2) -> rgb8
    // RGBx/BGRx: the 4th byte is undefined padding, published as alpha -- consumers that
    // care about alpha shouldn't be fed an x-format; the geometry/stride are what matter.
    case 12: case 14: out = {"rgba8", 4, false, Yuv::NONE}; return true;  // RGBA/RGBx (direct)
    case 13: case 15: out = {"bgra8", 4, false, Yuv::NONE}; return true;  // BGRA/BGRx (direct)
    default: return false;
  }
}

}  // namespace

class CamHeaderBridge : public CamBridgeBase {
 public:
  explicit CamHeaderBridge(const rclcpp::NodeOptions& options)
      : CamBridgeBase("cam_ros2_bridge", options, /*default_socket=*/"/tmp/cam/frames") {
    start_pipeline();
  }

 protected:
  std::string pipeline_desc() const override {
    return "shmsrc socket-path=" + socket_path_ + " is-live=true ! "
           "application/x-cam-frame ! "
           "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
  }

  bool extract(GstSample*, GstBuffer*, const GstMapInfo& map, FrameMeta& out) override {
    if (map.size < sizeof(FrameHeader)) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "short buffer (%zu)", map.size);
      return false;
    }
    FrameHeader hdr;
    std::memcpy(&hdr, map.data, sizeof(hdr));
    if (std::memcmp(hdr.magic, kMagic, 4) != 0 || hdr.version != kVersion) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "bad header (magic/version)");
      return false;
    }
    // header_len is wire data: bound it before it becomes a pointer offset (an oversized value
    // would underflow src_size below and read past the mapped shm region).
    if (hdr.header_len < sizeof(FrameHeader) || hdr.header_len > map.size) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000,
                           "bad header_len %u (buffer %zu)", hdr.header_len, map.size);
      return false;
    }
    PixInfo pix;
    if (!pixfmt_info(hdr.pixfmt, pix)) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "unknown pixfmt %u", hdr.pixfmt);
      return false;
    }
    const uint8_t* src = map.data + hdr.header_len;   // header_len = forward-compatible pixel offset
    const size_t src_size = map.size - hdr.header_len;
    out.width = hdr.width;
    out.height = hdr.height;
    if (pix.yuv != Yuv::NONE) {                        // color: convert the YUV plane to rgb8
      if (!yuv_to_rgb8(pix.yuv, src, src_size, hdr.width, hdr.height, convert_buf_)) {
        RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000,
                             "short color frame (pixfmt %u, %dx%d, %zu bytes)",
                             hdr.pixfmt, hdr.width, hdr.height, src_size);
        return false;
      }
      out.data = convert_buf_.data();
      out.size = convert_buf_.size();
      out.encoding = "rgb8";
    } else {
      out.data = src;
      out.size = src_size;
      // A bayer camera is labeled by the CAM_ROS_ENCODING hint (the header only knows GRAY8); mono/color
      // fall back to the header's pixfmt. image_proc (composed by the launch) does any debayering.
      out.encoding = (!encoding_.empty() && pix.bytes_per_px == 1) ? encoding_ : pix.encoding;
    }
    out.big_endian = pix.big_endian;
    out.stamp_ns = static_cast<int64_t>(hdr.timestamp_ns);
    out.frame_id = hdr.frame_id;
    return true;
  }
};

}  // namespace cam_ros2_bridge

RCLCPP_COMPONENTS_REGISTER_NODE(cam_ros2_bridge::CamHeaderBridge)
