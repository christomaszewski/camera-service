// CamBridgeBase: the shared machinery behind the two transport-specific bridge components.
//
// Both components are proper rclcpp composable nodes (RCLCPP_COMPONENTS_REGISTER_NODE) so they can
// be loaded into a `component_container_mt` and, on JP6, share a process with image_proc::DebayerNode
// for intra-process (zero-copy) debayering. The base owns everything that doesn't depend on the wire
// format: parameters, the image_transport publisher, the GStreamer appsink loop, and the publish step.
//
// What differs between transports lives behind two virtuals:
//   - pipeline_desc():  the gst-launch string (shmsrc+header on JP6, unixfdsrc[+bayer2rgb] on JP7).
//   - extract():        fill a FrameMeta from each appsink sample (parse the 36-byte header vs. read
//                       the negotiated caps + native buffer fields).
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>
#include <vector>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <image_transport/image_transport.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

namespace cam_ros2_bridge {

// One frame, normalized across transports. `data` points into the still-mapped GstBuffer (valid only
// for the duration of the extract()/publish() call); publish() copies it into the ROS message.
struct FrameMeta {
  const uint8_t* data = nullptr;   // start of the pixel plane (past any header)
  size_t size = 0;                 // bytes available at `data`
  int width = 0;
  int height = 0;
  std::string encoding;            // sensor_msgs encoding: mono8/mono16/bayer_*8/rgb8
  bool big_endian = false;
  int64_t stamp_ns = 0;            // absolute capture time (PTP epoch when locked)
  uint64_t frame_id = 0;
};

// sensor_msgs encoding -> bytes per pixel (for the row stride). Bayer is a single 8-bit plane.
int bytes_per_pixel(const std::string& encoding);

// YUV layouts the bridge converts to rgb8 -- sensor_msgs has no encoding for planar/semi-planar YUV,
// and the decode branch delivers I420 for every color (encoded/RTSP) source. NONE = a directly-mappable
// format (mono/rgb/bgr/rgba/bgra), handled without conversion.
enum class Yuv { NONE, I420, NV12, YUY2, UYVY, YV12, NV24 };

// Convert a YUV plane (full-range BT.601, as decoded MJPEG/JPEG produces) into a packed rgb8 buffer in
// `out` (resized to w*h*3). Returns false if `src_size` is too small for w*h in `fmt`.
bool yuv_to_rgb8(Yuv fmt, const uint8_t* src, size_t src_size, int w, int h, std::vector<uint8_t>& out);

// Env var value or a default (treats an empty value as unset).
const char* env_or(const char* key, const char* def);

class CamBridgeBase : public rclcpp::Node {
 public:
  ~CamBridgeBase() override;

 protected:
  // Subclasses pass their node name + the per-transport default socket path; `options` MUST be the
  // NodeOptions the component was loaded with (carries use_intra_process_comms etc.).
  CamBridgeBase(const std::string& node_name, const rclcpp::NodeOptions& options,
                 const std::string& default_socket_path);

  // Bring the GStreamer pipeline up. Call from the SUBCLASS constructor (after its own params are
  // declared) so the pipeline_desc()/extract() virtuals dispatch to the subclass, not the base.
  void start_pipeline();

  // Tear the dataflow down. Idempotent, and EVERY subclass destructor MUST call it as its first
  // statement -- see the note on ~CamUnixfdBridge/~CamHeaderBridge.
  //
  // Why it can't just live in ~CamBridgeBase: a base destructor runs AFTER the derived subobject is
  // already gone, so by then the vptr is the base's and extract() is pure virtual again. The appsink
  // still has emit-signals=true and is still connected, so any sample arriving in that window
  // dispatches a pure virtual -- "pure virtual method called", SIGABRT (exit 134), on any shutdown
  // that happens while frames are flowing. The old destructor made the window WIDER by joining the
  // bus thread (up to one 500 ms poll tick) before stopping the pipeline.
  //
  // extract() is deliberately left pure virtual rather than given a `return false` default: the
  // compile-time contract that a transport must implement it is worth more than a safety net for a
  // destructor that forgets this call.
  void stop_pipeline();

  // The gst-launch description; MUST contain `appsink name=sink`.
  virtual std::string pipeline_desc() const = 0;

  // Fill `out` from one mapped appsink sample. Return false to drop the frame (logged, throttled).
  virtual bool extract(GstSample* sample, GstBuffer* buf, const GstMapInfo& map, FrameMeta& out) = 0;

  // Common parameters (declared by the base; subclasses read them).
  std::string socket_path_;
  std::string topic_;
  std::string frame_id_;
  std::string encoding_;   // CAM_ROS_ENCODING hint: bayer_* for a CFA camera, "" for mono
  bool debayer_ = false;
  double publish_rate_ = 0.0;   // max publish rate in Hz; 0 = publish every frame
  std::vector<uint8_t> convert_buf_;   // scratch for YUV->rgb8 (reused per frame; single stream thread)

 private:
  static GstFlowReturn on_new_sample_static(GstAppSink* sink, gpointer self);
  GstFlowReturn on_new_sample(GstAppSink* sink);
  // True = drop this frame to hold `publish_rate`. Called on the (single) appsink streaming thread
  // BEFORE map/extract, so a dropped frame skips the YUV conversion and the message copy entirely.
  bool throttle_drop();
  void publish(const FrameMeta& m);

  int64_t min_period_ns_ = 0;          // 1e9 / publish_rate; 0 = unthrottled
  int64_t next_pub_ns_ = 0;            // steady-clock deadline for the next publish (0 = first frame)
  GstElement* pipeline_ = nullptr;
  GstElement* sink_ = nullptr;         // owned ref on the appsink; held so stop_pipeline can disconnect
  gulong sample_handler_ = 0;          // the new-sample connection, severed before teardown
  bool stopped_ = false;               // stop_pipeline() is idempotent (base + derived both call it)
  GstBus* bus_ = nullptr;              // watched for ERROR/EOS (producer restart) by bus_thread_
  std::thread bus_thread_;
  std::atomic<bool> stopping_{false};  // set by the destructor so the bus watcher exits quietly
  image_transport::Publisher pub_;
};

}  // namespace cam_ros2_bridge
