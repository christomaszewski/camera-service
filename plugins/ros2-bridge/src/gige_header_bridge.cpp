// GigeHeaderBridge (JP6 / GStreamer 1.20): consume the core's `application/x-gige-frame` shm endpoint
// and republish each frame as sensor_msgs/Image, with header.stamp from the per-frame hardware (PTP)
// timestamp carried in the 36-byte header. This is the legacy transport: shm drops caps/PTS/meta, so
// the core prepends a header. (JP7 uses the header-free unixfd transport -- see gige_unixfd_bridge.cpp.)
//
// Color on JP6: this component always publishes the mosaic labeled bayer_* (option A). When debayer is
// requested the launch file composes image_proc::DebayerNode into the same container (intra-process,
// zero-copy) -- so there is no in-process demosaic here anymore.
//
// The FrameHeader below MUST match gige_driver/transport.py (struct "<4sHHQQHHIBBH", 36 bytes, LE).
// Jetson (arm64) and x86 are both little-endian, so a packed struct maps the wire bytes directly.
#include <cstring>

#include <rclcpp_components/register_node_macro.hpp>

#include "gige_bridge_base.hpp"

namespace gige_ros2_bridge {
namespace {

constexpr char kMagic[4] = {'G', 'I', 'G', 'E'};
constexpr uint16_t kVersion = 1;

#pragma pack(push, 1)
struct FrameHeader {
  char     magic[4];     // "GIGE"
  uint16_t version;
  uint16_t header_len;   // offset to pixel data
  uint64_t timestamp_ns; // absolute capture time (ns); PTP epoch when locked
  uint64_t frame_id;
  uint16_t width;
  uint16_t height;
  uint32_t pixfmt;       // 1=GRAY8, 2=GRAY16_LE, 3=GRAY16_BE
  uint8_t  ts_source;    // 0=ptp_chunk, 1=camera, 2=system
  uint8_t  flags;
  uint16_t reserved;
};
#pragma pack(pop)
static_assert(sizeof(FrameHeader) == 36, "FrameHeader must match transport.py (36 bytes)");

struct PixInfo {
  const char* encoding;
  int bytes_per_px;
  bool big_endian;
};

bool pixfmt_info(uint32_t code, PixInfo& out) {
  switch (code) {
    case 1: out = {"mono8", 1, false}; return true;
    case 2: out = {"mono16", 2, false}; return true;
    case 3: out = {"mono16", 2, true};  return true;
    default: return false;
  }
}

}  // namespace

class GigeHeaderBridge : public GigeBridgeBase {
 public:
  explicit GigeHeaderBridge(const rclcpp::NodeOptions& options)
      : GigeBridgeBase("gige_ros2_bridge", options, /*default_socket=*/"/tmp/gige/frames") {
    start_pipeline();
  }

 protected:
  std::string pipeline_desc() const override {
    return "shmsrc socket-path=" + socket_path_ + " is-live=true ! "
           "application/x-gige-frame ! "
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
    PixInfo pix;
    if (!pixfmt_info(hdr.pixfmt, pix)) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "unknown pixfmt %u", hdr.pixfmt);
      return false;
    }
    out.data = map.data + hdr.header_len;          // header_len = forward-compatible pixel offset
    out.size = map.size - hdr.header_len;
    out.width = hdr.width;
    out.height = hdr.height;
    // A bayer camera is labeled by the GIGE_ROS_ENCODING hint (the header only knows GRAY8); mono falls
    // back to the header's pixfmt. image_proc (composed by the launch) does any debayering.
    out.encoding = (!encoding_.empty() && pix.bytes_per_px == 1) ? encoding_ : pix.encoding;
    out.big_endian = pix.big_endian;
    out.stamp_ns = static_cast<int64_t>(hdr.timestamp_ns);
    out.frame_id = hdr.frame_id;
    return true;
  }
};

}  // namespace gige_ros2_bridge

RCLCPP_COMPONENTS_REGISTER_NODE(gige_ros2_bridge::GigeHeaderBridge)
