cmake_minimum_required(VERSION 3.0.2)
project(gige_ros1_bridge)

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 17)
endif()

find_package(catkin REQUIRED COMPONENTS roscpp sensor_msgs image_transport)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED gstreamer-1.0 gstreamer-app-1.0)

catkin_package(CATKIN_DEPENDS roscpp sensor_msgs image_transport)

include_directories(${catkin_INCLUDE_DIRS} ${GST_INCLUDE_DIRS})

add_executable(gige_ros1_bridge src/gige_ros1_bridge.cpp)
target_compile_options(gige_ros1_bridge PRIVATE ${GST_CFLAGS_OTHER})
target_link_libraries(gige_ros1_bridge ${catkin_LIBRARIES} ${GST_LIBRARIES})

install(TARGETS gige_ros1_bridge RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION})
install(DIRECTORY launch DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION})
