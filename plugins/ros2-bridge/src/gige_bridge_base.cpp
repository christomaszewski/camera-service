#include "gige_bridge_base.hpp"

#include <cstdlib>
#include <stdexcept>

namespace gige_ros2_bridge {

int bytes_per_pixel(const std::string& enc) {
  if (enc == "rgb8" || enc == "bgr8") return 3;
  if (enc == "rgba8" || enc == "bgra8") return 4;
  // 16-bit single-plane formats: mono16 and the (unused-today) bayer_*16 variants.
  if (enc == "mono16" || (enc.rfind("bayer_", 0) == 0 && enc.size() >= 2 &&
                          enc.compare(enc.size() - 2, 2, "16") == 0)) {
    return 2;
  }
  // mono8 and all bayer_*8 are a single 8-bit plane.
  return 1;
}

const char* env_or(const char* key, const char* def) {
  const char* v = std::getenv(key);
  return (v && *v) ? v : def;
}

GigeBridgeBase::GigeBridgeBase(const std::string& node_name, const rclcpp::NodeOptions& options,
                               const std::string& default_socket_path)
    : rclcpp::Node(node_name, options) {
  // As a composable component there is no main() to gst_init() for us (component_container_mt knows
  // nothing about GStreamer), so do it here. gst_init is idempotent -> safe with multiple components
  // sharing the container. Without it the element registry is empty and parse_launch finds nothing.
  gst_init(nullptr, nullptr);
  socket_path_ = declare_parameter<std::string>("socket_path", default_socket_path);
  topic_ = declare_parameter<std::string>("topic", "image_raw");
  frame_id_ = declare_parameter<std::string>("frame_id", "camera");
  encoding_ = declare_parameter<std::string>("encoding", env_or("GIGE_ROS_ENCODING", ""));
  debayer_ = declare_parameter<bool>("debayer", std::string(env_or("GIGE_DEBAYER", "false")) == "true");

  // image_transport gives the raw topic + a lazy `<topic>/compressed` (JPEG/PNG via
  // compressed_image_transport) that only costs CPU when something subscribes.
  pub_ = image_transport::create_publisher(this, topic_, rclcpp::SensorDataQoS().get_rmw_qos_profile());
}

GigeBridgeBase::~GigeBridgeBase() {
  if (pipeline_) {
    gst_element_set_state(pipeline_, GST_STATE_NULL);
    gst_object_unref(pipeline_);
  }
}

void GigeBridgeBase::start_pipeline() {
  const std::string desc = pipeline_desc();
  RCLCPP_INFO(get_logger(), "pipeline: %s", desc.c_str());
  GError* err = nullptr;
  pipeline_ = gst_parse_launch(desc.c_str(), &err);
  if (!pipeline_) {
    const std::string m = err ? err->message : "unknown";
    if (err) g_error_free(err);
    throw std::runtime_error("failed to build pipeline: " + m);
  }
  GstElement* sink = gst_bin_get_by_name(GST_BIN(pipeline_), "sink");
  if (!sink) throw std::runtime_error("pipeline has no `appsink name=sink`");
  g_signal_connect(sink, "new-sample", G_CALLBACK(&GigeBridgeBase::on_new_sample_static), this);
  gst_object_unref(sink);
  gst_element_set_state(pipeline_, GST_STATE_PLAYING);
  RCLCPP_INFO(get_logger(), "consuming %s -> publishing '%s' (frame_id=%s, debayer=%s)",
              socket_path_.c_str(), topic_.c_str(), frame_id_.c_str(), debayer_ ? "true" : "false");
}

GstFlowReturn GigeBridgeBase::on_new_sample_static(GstAppSink* sink, gpointer self) {
  return static_cast<GigeBridgeBase*>(self)->on_new_sample(sink);
}

GstFlowReturn GigeBridgeBase::on_new_sample(GstAppSink* sink) {
  GstSample* sample = gst_app_sink_pull_sample(sink);
  if (!sample) return GST_FLOW_OK;
  GstBuffer* buf = gst_sample_get_buffer(sample);
  GstMapInfo map;
  if (buf && gst_buffer_map(buf, &map, GST_MAP_READ)) {
    FrameMeta meta;
    if (extract(sample, buf, map, meta)) {
      publish(meta);
    }
    gst_buffer_unmap(buf, &map);
  }
  gst_sample_unref(sample);
  return GST_FLOW_OK;
}

void GigeBridgeBase::publish(const FrameMeta& m) {
  const int bpp = bytes_per_pixel(m.encoding);
  const size_t step = static_cast<size_t>(m.width) * bpp;
  const size_t need = step * m.height;
  if (!m.data || m.size < need) {
    RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000,
                         "short frame: have %zu, need %zu (%s %dx%d)",
                         m.size, need, m.encoding.c_str(), m.width, m.height);
    return;
  }
  // Publish a ConstSharedPtr: with intra-process comms enabled (set by the container when this
  // component is loaded), a same-process subscriber -- e.g. image_proc::DebayerNode on JP6 -- receives
  // the shared buffer by pointer, no copy/serialization. The one copy here (GstBuffer -> message) is
  // unavoidable: the GstBuffer is recycled, and sensor_msgs/Image can't loan the mmap'd memory.
  auto msg = std::make_shared<sensor_msgs::msg::Image>();
  msg->header.stamp = rclcpp::Time(m.stamp_ns);   // PTP capture time when locked
  msg->header.frame_id = frame_id_;
  msg->height = static_cast<uint32_t>(m.height);
  msg->width = static_cast<uint32_t>(m.width);
  msg->encoding = m.encoding;
  msg->is_bigendian = m.big_endian ? 1 : 0;
  msg->step = static_cast<uint32_t>(step);
  msg->data.assign(m.data, m.data + need);
  pub_.publish(msg);   // raw on <topic>; compressed_image_transport adds <topic>/compressed on demand
}

}  // namespace gige_ros2_bridge
