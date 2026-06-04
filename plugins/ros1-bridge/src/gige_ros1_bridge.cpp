// gige_ros1_bridge: ROS 1 (Noetic) mirror of the ros2-bridge's GigeHeaderBridge. Consumes the core's
// `application/x-gige-frame` shm endpoint -- the JP6 / GStreamer-<1.24 transport, since ROS 1 ships
// GStreamer 1.16 which has no unixfd -- and republishes each frame as sensor_msgs/Image, with
// header.stamp from the per-frame hardware (PTP) timestamp carried in the 36-byte header.
//
// Color: publishes the mosaic labeled bayer_* (option A); for color, run a ROS 1 image_proc/debayer
// nodelet (the launch file does this when params/debayer is set). A mono camera publishes mono8/16.
//
// The FrameHeader below MUST match gige_driver/transport.py (struct "<4sHHQQHHIBBH", 36 bytes, LE) --
// identical to the ros2 bridge's. arm64 + x86 are little-endian, so the packed struct maps wire bytes.
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <image_transport/image_transport.h>
#include <ros/ros.h>
#include <sensor_msgs/Image.h>

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

const char* env_or(const char* key, const char* def) {
  const char* v = std::getenv(key);
  return (v && *v) ? v : def;
}

}  // namespace

class GigeRos1Bridge {
 public:
  GigeRos1Bridge(ros::NodeHandle& nh, ros::NodeHandle& pnh) {
    pnh.param<std::string>("socket_path", socket_path_, "/tmp/gige/frames");
    pnh.param<std::string>("topic", topic_, "image_raw");
    pnh.param<std::string>("frame_id", frame_id_, "camera");
    pnh.param<std::string>("encoding", encoding_, env_or("GIGE_ROS_ENCODING", ""));  // bayer_*; "" = mono
    it_.reset(new image_transport::ImageTransport(nh));
    // image_transport gives the raw topic + a lazy <topic>/compressed (JPEG/PNG via
    // compressed_image_transport) that only costs CPU when something subscribes.
    pub_ = it_->advertise(topic_, 1);
    start_pipeline();
  }

  ~GigeRos1Bridge() {
    if (pipeline_) {
      gst_element_set_state(pipeline_, GST_STATE_NULL);
      gst_object_unref(pipeline_);
    }
  }

 private:
  void start_pipeline() {
    const std::string desc =
        "shmsrc socket-path=" + socket_path_ + " is-live=true ! "
        "application/x-gige-frame ! "
        "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
    GError* err = nullptr;
    pipeline_ = gst_parse_launch(desc.c_str(), &err);
    if (!pipeline_) {
      const std::string m = err ? err->message : "unknown";
      if (err) g_error_free(err);
      throw std::runtime_error("failed to build pipeline: " + m);
    }
    GstElement* sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
    g_signal_connect(sink, "new-sample", G_CALLBACK(&GigeRos1Bridge::on_sample_static), this);
    gst_object_unref(sink);
    gst_element_set_state(pipeline_, GST_STATE_PLAYING);
    ROS_INFO("consuming %s -> publishing '%s' (frame_id=%s)",
             socket_path_.c_str(), topic_.c_str(), frame_id_.c_str());
  }

  static GstFlowReturn on_sample_static(GstAppSink* sink, gpointer self) {
    return static_cast<GigeRos1Bridge*>(self)->on_sample(sink);
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
    const size_t expected = static_cast<size_t>(hdr.width) * hdr.height * pix.bytes_per_px;
    if (size < pixel_off + expected) {
      ROS_WARN_THROTTLE(1.0, "buffer too small: %zu < %zu", size, pixel_off + expected);
      return;
    }
    // A bayer camera is labeled by the GIGE_ROS_ENCODING hint (the header only knows GRAY8); mono falls
    // back to the header's pixfmt. A ROS 1 image_proc/debayer (run by the launch) does any debayering.
    const std::string enc = (!encoding_.empty() && pix.bytes_per_px == 1) ? encoding_ : pix.encoding;

    sensor_msgs::Image msg;
    msg.header.stamp.fromNSec(hdr.timestamp_ns);  // PTP capture time (epoch ns); fits ros::Time
    msg.header.frame_id = frame_id_;
    msg.height = hdr.height;
    msg.width = hdr.width;
    msg.encoding = enc;
    msg.is_bigendian = pix.big_endian ? 1 : 0;
    msg.step = static_cast<uint32_t>(hdr.width) * pix.bytes_per_px;
    msg.data.assign(data + pixel_off, data + pixel_off + expected);
    pub_.publish(msg);  // raw on <topic>; compressed_image_transport adds <topic>/compressed on demand
  }

  std::string socket_path_, topic_, frame_id_, encoding_;
  GstElement* pipeline_ = nullptr;
  std::unique_ptr<image_transport::ImageTransport> it_;
  image_transport::Publisher pub_;
};

int main(int argc, char** argv) {
  gst_init(&argc, &argv);
  ros::init(argc, argv, "gige_ros1_bridge");
  ros::NodeHandle nh, pnh("~");
  try {
    GigeRos1Bridge bridge(nh, pnh);  // GStreamer streaming thread publishes; ROS spinner keeps us alive
    ros::spin();
  } catch (const std::exception& e) {
    ROS_FATAL("%s", e.what());
  }
  gst_deinit();
  return 0;
}
