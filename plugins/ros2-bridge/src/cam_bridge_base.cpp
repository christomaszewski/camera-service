#include "cam_bridge_base.hpp"

#include <unistd.h>

#include <cstdlib>
#include <stdexcept>

namespace cam_ros2_bridge {

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

namespace {
inline uint8_t clamp8(int v) { return v < 0 ? 0 : (v > 255 ? 255 : static_cast<uint8_t>(v)); }
// full-range BT.601 YUV -> RGB, fixed-point (/256): R=Y+1.402(V-128), G=Y-0.344(U-128)-0.714(V-128),
// B=Y+1.772(U-128). MJPEG/JPEG decode is full-range; H.264/H.265 (limited-range BT.709) is slightly off.
inline void yuv2rgb(int y, int u, int v, uint8_t* dst) {
  const int d = u - 128, e = v - 128;
  dst[0] = clamp8(y + ((359 * e) >> 8));
  dst[1] = clamp8(y - ((88 * d + 183 * e) >> 8));
  dst[2] = clamp8(y + ((454 * d) >> 8));
}
}  // namespace

bool yuv_to_rgb8(Yuv fmt, const uint8_t* src, size_t src_size, int w, int h, std::vector<uint8_t>& out) {
  if (w <= 0 || h <= 0 || fmt == Yuv::NONE) return false;
  const size_t wh = static_cast<size_t>(w) * h, cw = w / 2, ch = h / 2;
  const bool packed422 = (fmt == Yuv::YUY2 || fmt == Yuv::UYVY);
  const size_t need = packed422 ? wh * 2
                    : (fmt == Yuv::NV12 ? wh + wh / 2
                    : (fmt == Yuv::NV24 ? wh * 3 : wh + 2 * cw * ch));
  if (src_size < need) return false;
  out.resize(wh * 3);
  uint8_t* dst = out.data();
  if (packed422) {   // per 2 px: YUY2 [Y0 U Y1 V], UYVY [U Y0 V Y1]; row stride w*2
    const int yo = (fmt == Yuv::UYVY) ? 1 : 0;   // Y offset within a 2-byte pixel
    const int co = (fmt == Yuv::UYVY) ? 0 : 1;   // U offset within a 4-byte pair (V at +2)
    for (int y = 0; y < h; ++y) {
      const uint8_t* row = src + static_cast<size_t>(y) * w * 2;
      for (int x = 0; x < w; ++x) {
        const int pair = (x >> 1) * 4;
        yuv2rgb(row[x * 2 + yo], row[pair + co], row[pair + co + 2],
                dst + 3 * (static_cast<size_t>(y) * w + x));
      }
    }
    return true;
  }
  const uint8_t* yp = src;                      // Y plane
  const uint8_t* cp = src + wh;                 // I420/YV12: chroma planes | NV12/NV24: interleaved UV
  for (int y = 0; y < h; ++y) {
    for (int x = 0; x < w; ++x) {
      int u, v;
      if (fmt == Yuv::NV12) {
        const size_t i = static_cast<size_t>(y / 2) * w + (x / 2) * 2;   // UV row stride = w
        u = cp[i]; v = cp[i + 1];
      } else if (fmt == Yuv::NV24) {            // 4:4:4 interleaved UV, full resolution
        const size_t i = 2 * (static_cast<size_t>(y) * w + x);
        u = cp[i]; v = cp[i + 1];
      } else {                                  // planar 4:2:0 -- I420: U then V | YV12: V then U
        const size_t i = static_cast<size_t>(y / 2) * cw + (x / 2);
        const int first = cp[i], second = cp[cw * ch + i];
        u = (fmt == Yuv::YV12) ? second : first;
        v = (fmt == Yuv::YV12) ? first : second;
      }
      yuv2rgb(yp[static_cast<size_t>(y) * w + x], u, v, dst + 3 * (static_cast<size_t>(y) * w + x));
    }
  }
  return true;
}

const char* env_or(const char* key, const char* def) {
  const char* v = std::getenv(key);
  return (v && *v) ? v : def;
}

CamBridgeBase::CamBridgeBase(const std::string& node_name, const rclcpp::NodeOptions& options,
                               const std::string& default_socket_path)
    : rclcpp::Node(node_name, options) {
  // As a composable component there is no main() to gst_init() for us (component_container_mt knows
  // nothing about GStreamer), so do it here. gst_init is idempotent -> safe with multiple components
  // sharing the container. Without it the element registry is empty and parse_launch finds nothing.
  gst_init(nullptr, nullptr);
  socket_path_ = declare_parameter<std::string>("socket_path", default_socket_path);
  topic_ = declare_parameter<std::string>("topic", "image_raw");
  frame_id_ = declare_parameter<std::string>("frame_id", "camera");
  encoding_ = declare_parameter<std::string>("encoding", env_or("CAM_ROS_ENCODING", ""));
  debayer_ = declare_parameter<bool>("debayer", std::string(env_or("CAM_DEBAYER", "false")) == "true");

  // image_transport gives the raw topic + a lazy `<topic>/compressed` (JPEG/PNG via
  // compressed_image_transport) that only costs CPU when something subscribes.
  pub_ = image_transport::create_publisher(this, topic_, rclcpp::SensorDataQoS().get_rmw_qos_profile());
}

CamBridgeBase::~CamBridgeBase() {
  stopping_ = true;   // the watcher polls with a finite timeout, so join returns within one tick
  if (bus_thread_.joinable()) bus_thread_.join();
  if (bus_) gst_object_unref(bus_);
  if (pipeline_) {
    gst_element_set_state(pipeline_, GST_STATE_NULL);
    gst_object_unref(pipeline_);
  }
}

void CamBridgeBase::start_pipeline() {
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
  g_signal_connect(sink, "new-sample", G_CALLBACK(&CamBridgeBase::on_new_sample_static), this);
  gst_object_unref(sink);
  if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    throw std::runtime_error("pipeline failed to reach PLAYING (is the core up and serving " +
                             socket_path_ + "?)");
  }
  // Watch the bus: shmsrc/unixfdsrc post ERROR when the producer restarts (the core re-binds its
  // socket) and EOS when it stops. Without this the node stays alive but silently delivers nothing
  // -- invisible to the compose restart policy and any healthcheck. Recovery IS the container
  // restart (the entrypoint re-waits for the socket), so exit the process rather than trying to
  // rebuild a wedged GStreamer graph in place. _exit: no clean teardown wanted -- destructors can
  // block on that same wedged graph, and the OS reclaims everything on restart anyway.
  bus_ = gst_element_get_bus(pipeline_);
  bus_thread_ = std::thread([this] {
    // Finite-timeout poll, NOT an infinite timed_pop: gst_bus_set_flushing() does not wake a
    // blocked popper (it only sets a flag and drains the queue), so an infinite wait would
    // deadlock the destructor's join() on every CLEAN shutdown (no ERROR/EOS ever arrives).
    while (!stopping_) {
      GstMessage* msg = gst_bus_timed_pop_filtered(
          bus_, 500 * GST_MSECOND,
          static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS));
      if (!msg) continue;                              // timeout tick -> re-check stopping_
      if (stopping_) { gst_message_unref(msg); return; }
      if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
        GError* e = nullptr;
        gchar* dbg = nullptr;
        gst_message_parse_error(msg, &e, &dbg);
        RCLCPP_FATAL(get_logger(), "GStreamer ERROR: %s (%s) -- exiting; the container restart reconnects",
                     e ? e->message : "?", dbg ? dbg : "");
        if (e) g_error_free(e);
        g_free(dbg);
      } else {
        RCLCPP_FATAL(get_logger(), "transport EOS (producer stopped) -- exiting; the container restart reconnects");
      }
      gst_message_unref(msg);
      ::_exit(EXIT_FAILURE);
    }
  });
  RCLCPP_INFO(get_logger(), "consuming %s -> publishing '%s' (frame_id=%s, debayer=%s)",
              socket_path_.c_str(), topic_.c_str(), frame_id_.c_str(), debayer_ ? "true" : "false");
}

GstFlowReturn CamBridgeBase::on_new_sample_static(GstAppSink* sink, gpointer self) {
  return static_cast<CamBridgeBase*>(self)->on_new_sample(sink);
}

GstFlowReturn CamBridgeBase::on_new_sample(GstAppSink* sink) {
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

void CamBridgeBase::publish(const FrameMeta& m) {
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

}  // namespace cam_ros2_bridge
