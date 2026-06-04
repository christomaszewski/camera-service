// GigeUnixfdBridge (JP7 / GStreamer >= 1.24): consume the core's header-free `unixfdsink` endpoint and
// republish each frame as sensor_msgs/Image. unixfd carries native caps + buffer fields over SCM_RIGHTS,
// so there is no 36-byte header: the pixel format comes from the negotiated caps, the frame_id from
// buffer.offset, and the absolute (PTP) capture time from buffer.offset_end (the core puts it there
// because an absolute-ns PTS stalls downstream flow).
//
// Color on JP7: debayer happens IN THE GSTREAMER PIPELINE (`bayer2rgb`), not via a composed image_proc
// node -- so when params.debayer is set on a CFA camera this component publishes rgb8 directly.
#include <string>

#include <rclcpp_components/register_node_macro.hpp>

#include "gige_bridge_base.hpp"

namespace gige_ros2_bridge {
namespace {

// Map negotiated GStreamer caps -> (sensor_msgs encoding, big-endian, width, height). Returns false on
// an unrecognized format. Covers what the core emits (mono GRAY8/16, Bayer rggb/grbg/gbrg/bggr) plus RGB
// for the in-pipeline-debayered path.
bool caps_to_meta(GstCaps* caps, std::string& enc, bool& big_endian, int& w, int& h) {
  if (!caps || gst_caps_get_size(caps) == 0) return false;
  GstStructure* s = gst_caps_get_structure(caps, 0);
  const char* name = gst_structure_get_name(s);
  const char* fmt = gst_structure_get_string(s, "format");
  if (!name || !fmt) return false;
  gst_structure_get_int(s, "width", &w);
  gst_structure_get_int(s, "height", &h);
  big_endian = false;
  const std::string n = name, f = fmt;
  if (n == "video/x-bayer") {
    if (f == "rggb") enc = "bayer_rggb8";
    else if (f == "grbg") enc = "bayer_grbg8";
    else if (f == "gbrg") enc = "bayer_gbrg8";
    else if (f == "bggr") enc = "bayer_bggr8";
    else return false;
    return true;
  }
  if (n == "video/x-raw") {
    if (f == "GRAY8") { enc = "mono8"; return true; }
    if (f == "GRAY16_LE") { enc = "mono16"; big_endian = false; return true; }
    if (f == "GRAY16_BE") { enc = "mono16"; big_endian = true; return true; }
    if (f == "RGB") { enc = "rgb8"; return true; }
    if (f == "BGR") { enc = "bgr8"; return true; }
    return false;
  }
  return false;
}

}  // namespace

class GigeUnixfdBridge : public GigeBridgeBase {
 public:
  explicit GigeUnixfdBridge(const rclcpp::NodeOptions& options)
      : GigeBridgeBase("gige_ros2_bridge", options, /*default_socket=*/"/tmp/gige/unixfd") {
    start_pipeline();
  }

 protected:
  std::string pipeline_desc() const override {
    // Debayer in-pipeline only for a CFA camera (the GIGE_ROS_ENCODING hint is bayer_* there). bayer2rgb
    // reads the pattern from the input caps; videoconvert normalizes its RGBx output to a clean RGB
    // plane. Mono (or debayer off) is a straight passthrough -- the appsink reads the format off caps.
    std::string chain = "unixfdsrc socket-path=" + socket_path_ + " ! ";
    if (debayer_ && encoding_.rfind("bayer_", 0) == 0) {
      chain += "bayer2rgb ! videoconvert ! video/x-raw,format=RGB ! ";
    }
    chain += "appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false";
    return chain;
  }

  bool extract(GstSample* sample, GstBuffer* buf, const GstMapInfo& map, FrameMeta& out) override {
    std::string enc;
    bool be = false;
    int w = 0, h = 0;
    if (!caps_to_meta(gst_sample_get_caps(sample), enc, be, w, h)) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 1000, "unrecognized caps on unixfd sample");
      return false;
    }
    out.data = map.data;       // unixfd carries the bare plane (no header) -> offset 0
    out.size = map.size;
    out.width = w;
    out.height = h;
    out.encoding = enc;
    out.big_endian = be;
    // Native buffer fields the core set (and that survive the optional bayer2rgb/videoconvert transform):
    // frame_id in OFFSET, absolute capture ns in OFFSET_END. Guard against NONE after a transform.
    const guint64 off = GST_BUFFER_OFFSET(buf);
    const guint64 off_end = GST_BUFFER_OFFSET_END(buf);
    out.frame_id = (off == GST_BUFFER_OFFSET_NONE) ? 0 : off;
    out.stamp_ns = (off_end == GST_BUFFER_OFFSET_NONE) ? 0 : static_cast<int64_t>(off_end);
    return true;
  }
};

}  // namespace gige_ros2_bridge

RCLCPP_COMPONENTS_REGISTER_NODE(gige_ros2_bridge::GigeUnixfdBridge)
