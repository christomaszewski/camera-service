// GigeBridgeBase: the shared machinery behind the two transport-specific bridge components.
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

#include <cstdint>
#include <string>

#include <gst/gst.h>
#include <gst/app/gstappsink.h>

#include <image_transport/image_transport.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/image.hpp>

namespace gige_ros2_bridge {

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

// Env var value or a default (treats an empty value as unset).
const char* env_or(const char* key, const char* def);

class GigeBridgeBase : public rclcpp::Node {
 public:
  ~GigeBridgeBase() override;

 protected:
  // Subclasses pass their node name + the per-transport default socket path; `options` MUST be the
  // NodeOptions the component was loaded with (carries use_intra_process_comms etc.).
  GigeBridgeBase(const std::string& node_name, const rclcpp::NodeOptions& options,
                 const std::string& default_socket_path);

  // Bring the GStreamer pipeline up. Call from the SUBCLASS constructor (after its own params are
  // declared) so the pipeline_desc()/extract() virtuals dispatch to the subclass, not the base.
  void start_pipeline();

  // The gst-launch description; MUST contain `appsink name=sink`.
  virtual std::string pipeline_desc() const = 0;

  // Fill `out` from one mapped appsink sample. Return false to drop the frame (logged, throttled).
  virtual bool extract(GstSample* sample, GstBuffer* buf, const GstMapInfo& map, FrameMeta& out) = 0;

  // Common parameters (declared by the base; subclasses read them).
  std::string socket_path_;
  std::string topic_;
  std::string frame_id_;
  std::string encoding_;   // GIGE_ROS_ENCODING hint: bayer_* for a CFA camera, "" for mono
  bool debayer_ = false;

 private:
  static GstFlowReturn on_new_sample_static(GstAppSink* sink, gpointer self);
  GstFlowReturn on_new_sample(GstAppSink* sink);
  void publish(const FrameMeta& m);

  GstElement* pipeline_ = nullptr;
  image_transport::Publisher pub_;
};

}  // namespace gige_ros2_bridge
