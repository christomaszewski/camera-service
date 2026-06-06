cmake_minimum_required(VERSION 3.8)
project(cam_ros2_bridge)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_components REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(image_transport REQUIRED)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED IMPORTED_TARGET gstreamer-1.0 gstreamer-app-1.0)

# One shared library holds the base + both transport components, so they can be loaded into a
# component_container_mt (and share a process with image_proc on JP6 for intra-process debayering).
add_library(cam_bridge_components SHARED
  src/cam_bridge_base.cpp
  src/cam_header_bridge.cpp
  src/cam_unixfd_bridge.cpp)
target_include_directories(cam_bridge_components PRIVATE src)
# Modern CMake linkage (works on Jazzy + Lyrical). `ament_target_dependencies` was removed in Lyrical;
# link the exported targets directly -- they carry their include dirs transitively.
target_link_libraries(cam_bridge_components
  PkgConfig::GST
  rclcpp::rclcpp
  rclcpp_components::component
  ${sensor_msgs_TARGETS}
  image_transport::image_transport)

# Register both nodes so `component_container_mt` can load them by class name (writes the ament index
# resource the container reads).
rclcpp_components_register_nodes(cam_bridge_components
  "cam_ros2_bridge::CamHeaderBridge"
  "cam_ros2_bridge::CamUnixfdBridge")

install(TARGETS cam_bridge_components
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin)
install(DIRECTORY launch DESTINATION share/${PROJECT_NAME})

ament_package()
