# Core GigE Vision driver for Jetson AGX Orin. The base image is a build-arg (BASE_IMAGE):
#   default = JetPack 6 (l4t-jetpack / L4T r36.x / Ubuntu 22.04 / GStreamer 1.20)
#   JetPack 7 (Orin via JP7.2: Ubuntu 24.04 / GStreamer 1.24 / CUDA 13) -- pass a 24.04 base, e.g.
#     docker build --build-arg BASE_IMAGE=nvcr.io/nvidia/cuda:13.0.0-devel-ubuntu24.04 ...   (verify on-device)
#
# The NVIDIA GStreamer stack (nvv4l2h265enc, nvvidconv, ...) is injected at runtime by the nvidia
# container runtime. On JP7 confirm it reaches the container (docs/jetpack7-bringup.md); the software
# FFV1 recorder path does NOT depend on it. We add Aravis from source (>= 0.8.32, for extended-chunk
# payloads) + its GStreamer plugin + introspection. Build/run on the Jetson (arm64) under `runtime: nvidia`;
# verify `gst-inspect-1.0 aravissrc` and `python3 -c "import gi; gi.require_version('Aravis','0.8')"`.
ARG BASE_IMAGE=nvcr.io/nvidia/l4t-jetpack:r36.4.0
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
ARG ARAVIS_VERSION=0.8.34

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-yaml \
      gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
      meson ninja-build pkg-config cmake gettext git ca-certificates wget xz-utils \
      libglib2.0-dev libxml2-dev libusb-1.0-0-dev \
      gobject-introspection libgirepository1.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Build Aravis with the GStreamer plugin (aravissrc) + introspection (Aravis-0.8 typelib).
RUN cd /tmp \
    && wget -q "https://github.com/AravisProject/aravis/releases/download/${ARAVIS_VERSION}/aravis-${ARAVIS_VERSION}.tar.xz" \
    && tar xf "aravis-${ARAVIS_VERSION}.tar.xz" \
    && cd "aravis-${ARAVIS_VERSION}" \
    && meson setup build --prefix=/usr/local \
         -Dgst-plugin=enabled -Dintrospection=enabled -Dviewer=disabled -Dusb=enabled \
    && ninja -C build && ninja -C build install && ldconfig \
    && rm -rf /tmp/aravis-*

# Make the locally-installed Aravis typelib + libs + gst plugin discoverable.
ENV GI_TYPELIB_PATH=/usr/local/lib/aarch64-linux-gnu/girepository-1.0:/usr/lib/aarch64-linux-gnu/girepository-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:/usr/local/lib:${LD_LIBRARY_PATH} \
    GST_PLUGIN_PATH=/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0

WORKDIR /app
# The only Python dep is PyYAML, provided by apt python3-yaml above -- so no pip step (which would
# also hit PEP 668 / externally-managed-environment on Ubuntu 24.04 / JP7). gi/GStreamer/Aravis are
# system bindings, never pip. See requirements.txt for the (documentation-only) dep list.
COPY core-driver/ /app/

ENV GIGE_CONFIG=/app/config/camera.yaml
# Sensor container: the supervisor spawns the core + any enabled in-image plugins.
# (Override the command with `main.py -c ...` to run the core alone.)
ENTRYPOINT ["python3"]
CMD ["supervisor.py", "-c", "config/camera.yaml"]
