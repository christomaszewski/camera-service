// gige_ros2_bridge: consume the core's `application/x-gige-frame` shm endpoint and
// republish each frame as sensor_msgs/Image, with header.stamp taken from the
// hardware (PTP) timestamp carried in the frame header.
//
// The FrameHeader below MUST match gige_driver/transport.py (struct "<4sHHQQHHIBBH",
// 36 bytes, little-endian). Jetson (arm64) and x86 are both little-endian, so a packed
// struct maps the wire bytes directly; the static_assert guards against drift.
#include <cstdint>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <image_transport/image_transport.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

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

class GigeRos2Bridge : public rclcpp::Node {
 public:
  GigeRos2Bridge() : rclcpp::Node("gige_ros2_bridge") {
    socket_path_ = declare_parameter<std::string>("socket_path", "/tmp/gige/frames");
    topic_ = declare_parameter<std::string>("topic", "image_raw");
    frame_id_ = declare_parameter<std::string>("frame_id", "camera");
    encoding_ = declare_parameter<std::string>("encoding", "");  // "" = derive from pixfmt

    // image_transport gives us the raw topic + a lazy `<topic>/compressed` (JPEG/PNG via
    // compressed_image_transport) that only costs CPU when something subscribes to it.
    pub_ = image_transport::create_publisher(this, topic_, rclcpp::SensorDataQoS().get_rmw_qos_profile());
    start_pipeline();
  }

  ~GigeRos2Bridge() override {
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
    g_signal_connect(sink, "new-sample", G_CALLBACK(&GigeRos2Bridge::on_new_sample_static), this);
    gst_object_unref(sink);
    gst_element_set_state(pipeline_, GST_STATE_PLAYING);
    RCLCPP_INFO(get_logger(), "consuming %s -> publishing '%s' (frame_id=%s)",
                socket_path_.c_str(), topic_.c_str(), frame_id_.c_str());
  }

  static GstFlowReturn on_new_sample_static(GstAppSink* sink, gpointer self) {
    return static_cast<GigeRos2Bridge*>(self)->on_new_sample(sink);
  }

  GstFlowReturn on_new_sample(GstAppSink* sink) {
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
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "short buffer (%zu)", size);
      return;
    }
    FrameHeader hdr;
    std::memcpy(&hdr, data, sizeof(hdr));
    if (std::memcmp(hdr.magic, kMagic, 4) != 0 || hdr.version != kVersion) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "bad header (magic/version)");
      return;
    }
    PixInfo pix;
    if (!pixfmt_info(hdr.pixfmt, pix)) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "unknown pixfmt %u", hdr.pixfmt);
      return;
    }
    const size_t pixel_off = hdr.header_len;  // forward-compatible across header versions
    const size_t expected = static_cast<size_t>(hdr.width) * hdr.height * pix.bytes_per_px;
    if (size < pixel_off + expected) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000,
                           "buffer too small: %zu < %zu", size, pixel_off + expected);
      return;
    }

    sensor_msgs::msg::Image msg;
    msg.header.stamp = rclcpp::Time(static_cast<int64_t>(hdr.timestamp_ns));  // PTP capture time
    msg.header.frame_id = frame_id_;
    msg.height = hdr.height;
    msg.width = hdr.width;
    msg.encoding = encoding_.empty() ? pix.encoding : encoding_;
    msg.is_bigendian = pix.big_endian ? 1 : 0;
    msg.step = static_cast<uint32_t>(hdr.width) * pix.bytes_per_px;
    msg.data.assign(data + pixel_off, data + pixel_off + expected);
    pub_.publish(msg);  // raw on <topic>; compressed_image_transport adds <topic>/compressed on demand
  }

  std::string socket_path_, topic_, frame_id_, encoding_;
  GstElement* pipeline_ = nullptr;
  image_transport::Publisher pub_;
};

int main(int argc, char** argv) {
  gst_init(&argc, &argv);
  rclcpp::init(argc, argv);
  try {
    rclcpp::spin(std::make_shared<GigeRos2Bridge>());
  } catch (const std::exception& e) {
    RCLCPP_FATAL(rclcpp::get_logger("gige_ros2_bridge"), "%s", e.what());
  }
  rclcpp::shutdown();
  gst_deinit();
  return 0;
}
