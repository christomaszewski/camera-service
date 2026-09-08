from setuptools import setup

package_name = "cam_ros2_source"

setup(
    name=package_name,
    version="0.0.1",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="camera-service",
    maintainer_email="maintainer@example.com",
    description="A ROS 2 image topic -> a camera-service instance's shm input.",
    license="Apache-2.0",
    entry_points={"console_scripts": ["node = cam_ros2_source.node:main"]},
)
