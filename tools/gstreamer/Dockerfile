# Opt-in development upgrade. Keep Jetson images and the distro compatibility images intact.
# Build once per consumer, reusing the same cached GStreamer stage:
#   docker build -f tools/gstreamer/Dockerfile --build-arg BASE_IMAGE=cam-dev:jp6m -t cam-dev:gst1.28.7 .
#   docker build -f tools/gstreamer/Dockerfile --build-arg BASE_IMAGE=webrtc-bridge:jp6m -t webrtc-bridge:gst1.28.7 .
ARG BASE_IMAGE=cam-dev:jp6m
FROM ubuntu:26.04 AS gstreamer
COPY tools/gstreamer/build.sh tools/gstreamer/SHA256SUMS /build-gstreamer/
RUN bash /build-gstreamer/build.sh

FROM ${BASE_IMAGE}
# The overlay is compiled against 26.04. Fail instead of loading it into an older userspace.
RUN . /etc/os-release && test "$ID:$VERSION_ID" = ubuntu:26.04 \
    && apt-get update && apt-get install -y --no-install-recommends --no-upgrade libunwind8 libdw1t64 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=gstreamer /opt/gstreamer /opt/gstreamer
ENV PATH=/opt/gstreamer/bin:$PATH \
    LD_LIBRARY_PATH=/opt/gstreamer/lib:/usr/local/lib:/usr/local/lib/aarch64-linux-gnu:/usr/local/lib/x86_64-linux-gnu \
    GI_TYPELIB_PATH=/opt/gstreamer/lib/girepository-1.0 \
    GST_PLUGIN_PATH=/opt/gstreamer/lib/gstreamer-1.0:/usr/local/lib/gstreamer-1.0 \
    GST_PLUGIN_SYSTEM_PATH=/usr/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/lib/x86_64-linux-gnu/gstreamer-1.0 \
    PKG_CONFIG_PATH=/opt/gstreamer/lib/pkgconfig
COPY tools/gstreamer/check.py /opt/gstreamer/check.py
RUN python3 /opt/gstreamer/check.py
