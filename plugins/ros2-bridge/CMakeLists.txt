cmake_minimum_required(VERSION 3.8)
project(gige_ros2_bridge)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()
if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(image_transport REQUIRED)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED IMPORTED_TARGET gstreamer-1.0 gstreamer-app-1.0)

add_executable(gige_ros2_bridge src/gige_ros2_bridge.cpp)
# Modern CMake linkage (works on Jazzy + Lyrical). `ament_target_dependencies` was removed
# in Lyrical; link the exported targets directly -- they carry their include dirs transitively.
target_link_libraries(gige_ros2_bridge
  PkgConfig::GST
  rclcpp::rclcpp
  ${sensor_msgs_TARGETS}
  image_transport::image_transport)

install(TARGETS gige_ros2_bridge DESTINATION lib/${PROJECT_NAME})

ament_package()
