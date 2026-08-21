# RTSP consumer: reads the core's plugin endpoint (unixfd on JP7 / shm+header on JP6) and serves it
# as a standard RTSP stream via GstRtspServer (tools/rtsp_server.py). Pure apt GStreamer -- no Rust,
# no source builds -- so this is a single light stage. Ubuntu 24.04 = GStreamer 1.24 for BOTH
# variants (webrtc-bridge precedent): on JP6 the nvidia runtime CSV-mounts the BSP nvv4l2 encoders
# in, on JP7 the CDI device does; off-Jetson the auto-probe falls back to x264enc/x265enc.
#
# Build (from repo root):  docker build -f plugins/rtsp-bridge/Dockerfile -t rtsp-bridge .
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# plugins-base/good: videoconvert, rtp payloaders; plugins-bad: h26xparse, bayer2rgb, shmsrc,
# unixfdsrc (>= 1.24), x265enc, the rtsp-server lib; plugins-ugly: x264enc; libav: avdec_* for the
# in-container test consumer (tools/rtsp_consumer.py). gir1.2-gst-rtsp-server-1.0 = the GI binding
# rtsp_server.py drives. python3-numpy: the 16->8 percentile stretch (thermal_preview). kmod: the
# NVENC plugins are runtime-injected on Jetson (CSV on JP6, CDI on JP7) and their init runs `lsmod`
# (libnvtvmr) -- without kmod that aborts on r38; same bake as the core + webrtc images. onnx
# element stripped as elsewhere.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      gir1.2-gst-rtsp-server-1.0 \
      python3 python3-gi python3-pip python3-numpy \
      gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      ca-certificates kmod \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
# eclipse-zenoh: the Python Zenoh binding for the fleet-discovery advertiser (presence + descriptor;
# docs/DISCOVERY.md). A prebuilt manylinux abi3 wheel installs on arm64/x86 -- no Rust build. PEP668
# externally-managed env in-container -> --break-system-packages.
RUN pip3 install --break-system-packages --no-cache-dir eclipse-zenoh==1.9.0

WORKDIR /app
COPY plugins/rtsp-bridge/ /app/
CMD ["bash", "run.sh"]
