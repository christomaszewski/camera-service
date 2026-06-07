// cam_ros1_bridge: ROS 1 (Noetic) mirror of the ros2-bridge's CamHeaderBridge. Consumes the core's
// `application/x-cam-frame` shm endpoint -- the JP6 / GStreamer-<1.24 transport, since ROS 1 ships
// GStreamer 1.16 which has no unixfd -- and republishes each frame as sensor_msgs/Image, with
// header.stamp from the per-frame hardware (PTP) timestamp carried in the 36-byte header.
//
// Color: publishes the mosaic labeled bayer_* (option A); for color, run a ROS 1 image_proc/debayer
// nodelet (the launch file does this when params/debayer is set). A mono camera publishes mono8/16.
//
// The FrameHeader below MUST match cam_driver/transport.py (struct "<4sHHQQHHIBBH", 36 bytes, LE) --
// identical to the ros2 bridge's. arm64 + x86 are little-endian, so the packed struct maps wire bytes.
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <image_transport/image_transport.h>
#include <ros/ros.h>
#include <sensor_msgs/Image.h>

namespace {

constexpr char kMagic[4] = {'C', 'A', 'M', 'F'};
constexpr uint16_t kVersion = 1;

// YUV layouts converted to rgb8 (sensor_msgs has no planar/semi-planar YUV encoding); NONE = direct.
// Mirrors cam_ros2_bridge::yuv_to_rgb8 -- the two bridges are separate packages, so the small converter
// is duplicated (as are FrameHeader/pixfmt_info). The core pushes tight (width-stride) buffers.
enum class Yuv { NONE, I420, NV12, YUY2 };

inline uint8_t clamp8(int v) { return v < 0 ? 0 : (v > 255 ? 255 : static_cast<uint8_t>(v)); }
inline void yuv2rgb(int y, int u, int v, uint8_t* dst) {   // full-range BT.601, fixed-point (/256)
  const int d = u - 128, e = v - 128;
  dst[0] = clamp8(y + ((359 * e) >> 8));
  dst[1] = clamp8(y - ((88 * d + 183 * e) >> 8));
  dst[2] = clamp8(y + ((454 * d) >> 8));
}
bool yuv_to_rgb8(Yuv fmt, const uint8_t* src, size_t src_size, int w, int h, std::vector<uint8_t>& out) {
  if (w <= 0 || h <= 0 || fmt == Yuv::NONE) return false;
  const size_t wh = static_cast<size_t>(w) * h, cw = w / 2, ch = h / 2;
  const size_t need = (fmt == Yuv::YUY2) ? wh * 2 : (fmt == Yuv::NV12 ? wh + wh / 2 : wh + 2 * cw * ch);
  if (src_size < need) return false;
  out.resize(wh * 3);
  uint8_t* dst = out.data();
  if (fmt == Yuv::YUY2) {                       // packed [Y0 U Y1 V] per 2 px, row stride w*2
    for (int y = 0; y < h; ++y) {
      const uint8_t* row = src + static_cast<size_t>(y) * w * 2;
      for (int x = 0; x < w; ++x) {
        const int pair = (x >> 1) * 4;
        yuv2rgb(row[x * 2], row[pair + 1], row[pair + 3], dst + 3 * (static_cast<size_t>(y) * w + x));
      }
    }
    return true;
  }
  const uint8_t* yp = src;
  const uint8_t* cp = src + wh;                  // I420: U plane | NV12: interleaved UV
  for (int y = 0; y < h; ++y) {
    for (int x = 0; x < w; ++x) {
      int u, v;
      if (fmt == Yuv::NV12) {
        const size_t i = static_cast<size_t>(y / 2) * w + (x / 2) * 2;
        u = cp[i]; v = cp[i + 1];
      } else {
        const size_t i = static_cast<size_t>(y / 2) * cw + (x / 2);
        u = cp[i]; v = cp[cw * ch + i];
      }
      yuv2rgb(yp[static_cast<size_t>(y) * w + x], u, v, dst + 3 * (static_cast<size_t>(y) * w + x));
    }
  }
  return true;
}

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
  Yuv yuv;                // != NONE -> convert the source plane to rgb8
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
    default: return false;
  }
}

const char* env_or(const char* key, const char* def) {
  const char* v = std::getenv(key);
  return (v && *v) ? v : def;
}

}  // namespace

class CamRos1Bridge {
 public:
  CamRos1Bridge(ros::NodeHandle& nh, ros::NodeHandle& pnh) {
    pnh.param<std::string>("socket_path", socket_path_, "/tmp/cam/frames");
    pnh.param<std::string>("topic", topic_, "image_raw");
    pnh.param<std::string>("frame_id", frame_id_, "camera");
    pnh.param<std::string>("encoding", encoding_, env_or("CAM_ROS_ENCODING", ""));  // bayer_*; "" = mono
    it_.reset(new image_transport::ImageTransport(nh));
    // image_transport gives the raw topic + a lazy <topic>/compressed (JPEG/PNG via
    // compressed_image_transport) that only costs CPU when something subscribes.
    pub_ = it_->advertise(topic_, 1);
    start_pipeline();
  }

  ~CamRos1Bridge() {
    if (pipeline_) {
      gst_element_set_state(pipeline_, GST_STATE_NULL);
      gst_object_unref(pipeline_);
    }
  }

 private:
  void start_pipeline() {
    const std::string desc =
        "shmsrc socket-path=" + socket_path_ + " is-live=true ! "
        "application/x-cam-frame ! "
        "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
    GError* err = nullptr;
    pipeline_ = gst_parse_launch(desc.c_str(), &err);
    if (!pipeline_) {
      const std::string m = err ? err->message : "unknown";
      if (err) g_error_free(err);
      throw std::runtime_error("failed to build pipeline: " + m);
    }
    GstElement* sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
    g_signal_connect(sink, "new-sample", G_CALLBACK(&CamRos1Bridge::on_sample_static), this);
    gst_object_unref(sink);
    gst_element_set_state(pipeline_, GST_STATE_PLAYING);
    ROS_INFO("consuming %s -> publishing '%s' (frame_id=%s)",
             socket_path_.c_str(), topic_.c_str(), frame_id_.c_str());
  }

  static GstFlowReturn on_sample_static(GstAppSink* sink, gpointer self) {
    return static_cast<CamRos1Bridge*>(self)->on_sample(sink);
  }

  GstFlowReturn on_sample(GstAppSink* sink) {
    GstSample* sample = gst_app_sink_pull_sample(sink);
    if (!sample) return GST_FLOW_OK;
    GstBuffer* buf = gst_sample_get_buffer(sample);
    GstMapInfo map;
    if (buf && gst_buffer_map(buf, &map, GST_MAP_READ)) {
      publish(map.data, map.size);
      gst_buffer_unmap(buf, &map);
    }
    gst_sample_unref(sample);
    return GST_FLOW_OK;
  }

  void publish(const uint8_t* data, size_t size) {
    if (size < sizeof(FrameHeader)) {
      ROS_WARN_THROTTLE(1.0, "short buffer (%zu)", size);
      return;
    }
    FrameHeader hdr;
    std::memcpy(&hdr, data, sizeof(hdr));
    if (std::memcmp(hdr.magic, kMagic, 4) != 0 || hdr.version != kVersion) {
      ROS_WARN_THROTTLE(1.0, "bad header (magic/version)");
      return;
    }
    PixInfo pix;
    if (!pixfmt_info(hdr.pixfmt, pix)) {
      ROS_WARN_THROTTLE(1.0, "unknown pixfmt %u", hdr.pixfmt);
      return;
    }
    const size_t pixel_off = hdr.header_len;  // forward-compatible across header versions
    sensor_msgs::Image msg;
    msg.header.stamp.fromNSec(hdr.timestamp_ns);  // PTP capture time (epoch ns); fits ros::Time
    msg.header.frame_id = frame_id_;
    msg.height = hdr.height;
    msg.width = hdr.width;
    msg.is_bigendian = pix.big_endian ? 1 : 0;
    if (pix.yuv != Yuv::NONE) {                  // color: convert the YUV plane to rgb8
      std::vector<uint8_t> rgb;
      if (!yuv_to_rgb8(pix.yuv, data + pixel_off, size - pixel_off, hdr.width, hdr.height, rgb)) {
        ROS_WARN_THROTTLE(1.0, "short color frame (pixfmt %u, %ux%u, %zu bytes)",
                          hdr.pixfmt, hdr.width, hdr.height, size - pixel_off);
        return;
      }
      msg.encoding = "rgb8";
      msg.step = static_cast<uint32_t>(hdr.width) * 3;
      msg.data = std::move(rgb);
    } else {
      const size_t expected = static_cast<size_t>(hdr.width) * hdr.height * pix.bytes_per_px;
      if (size < pixel_off + expected) {
        ROS_WARN_THROTTLE(1.0, "buffer too small: %zu < %zu", size, pixel_off + expected);
        return;
      }
      // A bayer camera is labeled by the CAM_ROS_ENCODING hint (the header only knows GRAY8); mono/color
      // fall back to the header's pixfmt. A ROS 1 image_proc/debayer (run by the launch) does debayering.
      msg.encoding = (!encoding_.empty() && pix.bytes_per_px == 1) ? encoding_ : pix.encoding;
      msg.step = static_cast<uint32_t>(hdr.width) * pix.bytes_per_px;
      msg.data.assign(data + pixel_off, data + pixel_off + expected);
    }
    pub_.publish(msg);  // raw on <topic>; compressed_image_transport adds <topic>/compressed on demand
  }

  std::string socket_path_, topic_, frame_id_, encoding_;
  GstElement* pipeline_ = nullptr;
  std::unique_ptr<image_transport::ImageTransport> it_;
  image_transport::Publisher pub_;
};

int main(int argc, char** argv) {
  gst_init(&argc, &argv);
  ros::init(argc, argv, "cam_ros1_bridge");
  ros::NodeHandle nh, pnh("~");
  try {
    CamRos1Bridge bridge(nh, pnh);  // GStreamer streaming thread publishes; ROS spinner keeps us alive
    ros::spin();
  } catch (const std::exception& e) {
    ROS_FATAL("%s", e.what());
  }
  gst_deinit();
  return 0;
}
