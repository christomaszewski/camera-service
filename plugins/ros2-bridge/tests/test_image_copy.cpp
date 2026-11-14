// Exercise the actual component library: both wire formats -> ROS message-owned RGB pixels.
#include "cam_bridge_base.hpp"

#include <class_loader/class_loader.hpp>
#include <rclcpp_components/node_factory.hpp>
#include <gst/app/gstappsrc.h>
#include <gst/allocators/gstfdmemory.h>

#include <array>
#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

using cam_ros2_bridge::Yuv;
using Bytes = std::vector<uint8_t>;
using namespace std::chrono_literals;

namespace {
void require(bool ok, const std::string& why) {
  if (!ok) throw std::runtime_error(why);
}

const Bytes kY{0, 64, 128, 255, 255, 128, 64, 0};
// Golden full-range BT.601 fixed-point results, including clipping and negative coefficients.
const Bytes kRgb{157, 0, 0, 221, 23, 0, 0, 170, 255, 97, 255, 255,
                 255, 214, 56, 255, 87, 0, 0, 106, 255, 0, 42, 198};
struct Format { Yuv yuv; const char* caps; uint32_t code; };
const std::array<Format, 6> kFormats{{
  {Yuv::I420, "I420", 4}, {Yuv::NV12, "NV12", 5}, {Yuv::YUY2, "YUY2", 6},
  {Yuv::UYVY, "UYVY", 11}, {Yuv::YV12, "YV12", 10}, {Yuv::NV24, "NV24", 9}}};

Bytes pixels(Yuv fmt) {
  Bytes out = kY;
  if (fmt == Yuv::I420) out.insert(out.end(), {16, 240, 240, 16});
  if (fmt == Yuv::YV12) out.insert(out.end(), {240, 16, 16, 240});
  if (fmt == Yuv::NV12) out.insert(out.end(), {16, 240, 240, 16});
  if (fmt == Yuv::NV24) {
    for (size_t i = 0; i < kY.size(); ++i) {
      const bool right = i % 4 >= 2;
      out.insert(out.end(), {uint8_t(right ? 240 : 16), uint8_t(right ? 16 : 240)});
    }
  }
  if (fmt == Yuv::YUY2 || fmt == Yuv::UYVY) {
    out.clear();
    for (size_t i = 0; i < kY.size(); i += 2) {
      const uint8_t u = i % 4 ? 240 : 16, v = i % 4 ? 16 : 240;
      if (fmt == Yuv::YUY2) out.insert(out.end(), {kY[i], u, kY[i + 1], v});
      else out.insert(out.end(), {u, kY[i], v, kY[i + 1]});
    }
  }
  return out;
}

void converter_checks() {
  for (const auto& fmt : kFormats) {
    const auto src = pixels(fmt.yuv);
    Bytes out(24, 99);
    const auto* storage = out.data();
    require(cam_ros2_bridge::yuv_to_rgb8(fmt.yuv, src.data(), src.size(), 4, 2, out), fmt.caps);
    require(out == kRgb, std::string(fmt.caps) + " changed color bytes");
    require(out.data() == storage, "converter must write into caller-owned storage");
    for (size_t n = 0; n < src.size(); ++n) {
      require(!cam_ros2_bridge::yuv_to_rgb8(fmt.yuv, src.data(), n, 4, 2, out),
              std::string(fmt.caps) + " accepted a short input");
      require(out == kRgb, "short input modified the output");
    }
  }
}

void append_le(Bytes& out, uint64_t value, size_t n) {
  for (size_t i = 0; i < n; ++i) out.push_back(static_cast<uint8_t>(value >> (8 * i)));
}

Bytes header(const Bytes& data, uint32_t code, uint64_t stamp) {
  Bytes out{'C', 'A', 'M', 'F'};
  append_le(out, 1, 2); append_le(out, 36, 2); append_le(out, stamp, 8);
  append_le(out, 17, 8); append_le(out, 4, 2); append_le(out, 2, 2);
  append_le(out, code, 4); append_le(out, 2, 1); append_le(out, 0, 1); append_le(out, 0, 2);
  out.insert(out.end(), data.begin(), data.end());
  return out;
}

struct Producer {
  GstElement* pipeline = nullptr;
  GstElement* source = nullptr;
  bool unixfd;
  std::string path;
  uint64_t seq = 0;

  explicit Producer(bool fd) : unixfd(fd) {
    path = "/tmp/cam-bridge-copy-" + std::to_string(getpid()) + (fd ? "-fd" : "-header");
    const std::string sink = fd ? "unixfdsink" : "shmsink wait-for-connection=false";
    const std::string desc = "appsrc name=source is-live=true format=time ! " + sink +
                             " socket-path=" + path + " sync=false";
    GError* error = nullptr;
    pipeline = gst_parse_launch(desc.c_str(), &error);
    const std::string detail = error ? error->message : "";
    if (error) g_error_free(error);
    require(pipeline && detail.empty(), "producer pipeline: " + detail);
    source = gst_bin_get_by_name(GST_BIN(pipeline), "source");
    require(gst_element_set_state(pipeline, GST_STATE_PLAYING) != GST_STATE_CHANGE_FAILURE,
            "producer did not start");
  }
  ~Producer() {
    if (pipeline) gst_element_set_state(pipeline, GST_STATE_NULL);
    if (source) gst_object_unref(source);
    if (pipeline) gst_object_unref(pipeline);
    unlink(path.c_str());
  }
  void push(const Bytes& pixels, const char* format, uint32_t code, uint64_t stamp) {
    const auto data = unixfd ? pixels : header(pixels, code, stamp);
    const std::string caps_text = unixfd ? std::string("video/x-raw,format=") + format +
        ",width=4,height=2,framerate=30/1" : "application/x-cam-frame";
    GstCaps* caps = gst_caps_from_string(caps_text.c_str());
    gst_app_src_set_caps(GST_APP_SRC(source), caps);
    gst_caps_unref(caps);
    GstBuffer* buffer = nullptr;
    if (unixfd) {
      // FD-backed input also works on 1.24, whose unixfdsink cannot pool ordinary memory.
      char name[] = "/tmp/cam-bridge-copy-pixels-XXXXXX";
      const int fd = mkstemp(name);
      require(fd >= 0, "pixel fd creation failed");
      unlink(name);
      require(write(fd, data.data(), data.size()) == static_cast<ssize_t>(data.size()), "pixel write failed");
      GstAllocator* allocator = gst_fd_allocator_new();
      GstMemory* mem = gst_fd_allocator_alloc(allocator, fd, data.size(), GST_FD_MEMORY_FLAG_NONE);
      gst_object_unref(allocator);
      buffer = gst_buffer_new();
      gst_buffer_append_memory(buffer, mem);
    } else {
      buffer = gst_buffer_new_allocate(nullptr, data.size(), nullptr);
      gst_buffer_fill(buffer, 0, data.data(), data.size());
    }
    GST_BUFFER_PTS(buffer) = seq++ * GST_SECOND / 30;
    GST_BUFFER_OFFSET(buffer) = 17;
    GST_BUFFER_OFFSET_END(buffer) = stamp;
    require(gst_app_src_push_buffer(GST_APP_SRC(source), buffer) == GST_FLOW_OK, "producer push failed");
  }
};

void component_checks(class_loader::ClassLoader& loader, bool unixfd) {
  Producer producer(unixfd);  // outlives the bridge; stopping the writer first intentionally exits it
  const auto until = std::chrono::steady_clock::now() + 3s;
  while (access(producer.path.c_str(), F_OK) != 0 && std::chrono::steady_clock::now() < until)
    std::this_thread::sleep_for(10ms);
  require(access(producer.path.c_str(), F_OK) == 0, "producer socket unavailable");
  const std::string component = unixfd ? "CamUnixfdBridge" : "CamHeaderBridge";
  std::shared_ptr<rclcpp_components::NodeFactory> factory;
  for (const auto& name : loader.getAvailableClasses<rclcpp_components::NodeFactory>()) {
    if (name.find(component) != std::string::npos)
      factory = loader.createInstance<rclcpp_components::NodeFactory>(name);
  }
  require(bool(factory), component + " factory unavailable");
  rclcpp::NodeOptions options;
  options.parameter_overrides({rclcpp::Parameter("socket_path", producer.path),
      rclcpp::Parameter("topic", "/copy_test"), rclcpp::Parameter("frame_id", "copy_test_camera"),
      rclcpp::Parameter("idle_gate", false)});
  auto bridge = factory->create_node_instance(options);
  auto reader = std::make_shared<rclcpp::Node>("copy_test_reader");
  sensor_msgs::msg::Image::ConstSharedPtr latest;
  auto sub = reader->create_subscription<sensor_msgs::msg::Image>("/copy_test", rclcpp::SensorDataQoS(),
      [&](sensor_msgs::msg::Image::ConstSharedPtr msg) { latest = std::move(msg); });
  rclcpp::executors::SingleThreadedExecutor executor;
  executor.add_node(reader);
  executor.add_node(bridge.get_node_base_interface());
  uint64_t stamp = 1234567890123;
  auto send = [&](const Bytes& data, const char* format, uint32_t code, bool accepted) {
    ++stamp;
    const auto deadline = std::chrono::steady_clock::now() + (accepted ? 3s : 300ms);
    do {
      producer.push(data, format, code, stamp);
      executor.spin_some();
      if (latest && static_cast<uint64_t>(rclcpp::Time(latest->header.stamp).nanoseconds()) == stamp) break;
      std::this_thread::sleep_for(20ms);
    } while (std::chrono::steady_clock::now() < deadline);
    if (!accepted) {
      require(!latest || static_cast<uint64_t>(rclcpp::Time(latest->header.stamp).nanoseconds()) != stamp,
              component + " published a short frame");
      return;
    }
    require(latest && static_cast<uint64_t>(rclcpp::Time(latest->header.stamp).nanoseconds()) == stamp,
            component + " did not publish " + format);
    require(latest->width == 4 && latest->height == 2 && latest->step == 12 &&
            latest->encoding == "rgb8" && latest->is_bigendian == 0 &&
            latest->header.frame_id == "copy_test_camera", component + " metadata changed");
    require(latest->data == kRgb, component + " pixel bytes changed for " + format);
  };
  for (const auto& fmt : kFormats) {
    auto data = pixels(fmt.yuv);
    send(data, fmt.caps, fmt.code, true);
    const auto retained = latest;  // remains valid through later mapping/recycling and publications
    data.pop_back();
    send(data, fmt.caps, fmt.code, false);
    require(retained->data == kRgb, "recycled input corrupted a retained ROS message");
  }
  send(kRgb, "RGB", 7, true);  // direct-copy formats still use the same publication path
  send(Bytes(kRgb.begin(), kRgb.end() - 1), "RGB", 7, false);
  executor.remove_node(bridge.get_node_base_interface());
  executor.remove_node(reader);
}
}  // namespace

int main(int argc, char** argv) {
  try {
    require(argc == 2, "pass the component library path");
    gst_init(nullptr, nullptr);
    converter_checks();
    rclcpp::init(0, nullptr);
    class_loader::ClassLoader loader(argv[1]);
    component_checks(loader, false);
    GstElementFactory* unixfd = gst_element_factory_find("unixfdsink");
    if (unixfd) {
      gst_object_unref(unixfd);
      component_checks(loader, true);
    } else {
      std::cout << "unixfd integration unavailable on this GStreamer runtime\n";
    }
    rclcpp::shutdown();
    std::cout << "image copy tests passed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    if (rclcpp::ok()) rclcpp::shutdown();
    return 1;
  }
}
