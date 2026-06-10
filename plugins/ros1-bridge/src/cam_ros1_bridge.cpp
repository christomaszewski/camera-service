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
#include <unistd.h>

#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
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
enum class Yuv { NONE, I420, NV12, YUY2, UYVY, YV12, NV24 };

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
  const uint8_t* yp = src;
  const uint8_t* cp = src + wh;                  // I420/YV12: chroma planes | NV12/NV24: interleaved UV
  for (int y = 0; y < h; ++y) {
    for (int x = 0; x < w; ++x) {
      int u, v;
      if (fmt == Yuv::NV12) {
        const size_t i = static_cast<size_t>(y / 2) * w + (x / 2) * 2;
        u = cp[i]; v = cp[i + 1];
      } else if (fmt == Yuv::NV24) {             // 4:4:4 interleaved UV, full resolution
        const size_t i = 2 * (static_cast<size_t>(y) * w + x);
        u = cp[i]; v = cp[i + 1];
      } else {                                   // planar 4:2:0 -- I420: U then V | YV12: V then U
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
    stopping_ = true;   // the watcher polls with a finite timeout, so join returns within one tick
    if (bus_thread_.joinable()) bus_thread_.join();
    if (bus_) gst_object_unref(bus_);
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
    if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
      throw std::runtime_error("pipeline failed to reach PLAYING (is the core up and serving " +
                               socket_path_ + "?)");
    }
    // Watch the bus: shmsrc posts ERROR when the producer restarts and EOS when it stops. Without
    // this the node stays alive but silently delivers nothing -- invisible to the compose restart
    // policy. Recovery IS the container restart (the entrypoint re-waits for the socket), so exit
    // the process; _exit because destructors can block on the same wedged graph. (Mirrors the
    // ros2-bridge's CamBridgeBase.)
    bus_ = gst_element_get_bus(pipeline_);
    bus_thread_ = std::thread([this] {
      // Finite-timeout poll, NOT an infinite timed_pop: gst_bus_set_flushing() does not wake a
      // blocked popper (it only sets a flag and drains the queue), so an infinite wait would
      // deadlock the destructor's join() on every CLEAN shutdown (no ERROR/EOS ever arrives).
      while (!stopping_) {
        GstMessage* msg = gst_bus_timed_pop_filtered(
            bus_, 500 * GST_MSECOND,
            static_cast<GstMessageType>(GST_MESSAGE_ERROR | GST_MESSAGE_EOS));
        if (!msg) continue;                            // timeout tick -> re-check stopping_
        if (stopping_) { gst_message_unref(msg); return; }
        if (GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR) {
          GError* e = nullptr;
          gchar* dbg = nullptr;
          gst_message_parse_error(msg, &e, &dbg);
          ROS_FATAL("GStreamer ERROR: %s (%s) -- exiting; the container restart reconnects",
                    e ? e->message : "?", dbg ? dbg : "");
          if (e) g_error_free(e);
          g_free(dbg);
        } else {
          ROS_FATAL("transport EOS (producer stopped) -- exiting; the container restart reconnects");
        }
        gst_message_unref(msg);
        ::_exit(EXIT_FAILURE);
      }
    });
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
    // header_len is wire data: bound it before it becomes a pointer offset (an oversized value
    // would underflow `size - pixel_off` on the color path and read past the mapped shm region).
    if (hdr.header_len < sizeof(FrameHeader) || hdr.header_len > size) {
      ROS_WARN_THROTTLE(1.0, "bad header_len %u (buffer %zu)", hdr.header_len, size);
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
  GstBus* bus_ = nullptr;              // watched for ERROR/EOS (producer restart) by bus_thread_
  std::thread bus_thread_;
  std::atomic<bool> stopping_{false};  // set by the destructor so the bus watcher exits quietly
  std::unique_ptr<image_transport::ImageTransport> it_;
  image_transport::Publisher pub_;
};

int main(int argc, char** argv) {
  gst_init(&argc, &argv);
  ros::init(argc, argv, "cam_ros1_bridge");
  ros::NodeHandle nh, pnh("~");
  int rc = 0;
  try {
    CamRos1Bridge bridge(nh, pnh);  // GStreamer streaming thread publishes; ROS spinner keeps us alive
    ros::spin();
  } catch (const std::exception& e) {
    ROS_FATAL("%s", e.what());
    rc = 1;                         // startup failure must exit non-zero so the restart policy retries
  }
  gst_deinit();
  return rc;
}
