# Core GigE Vision driver — multistage so the shipped image carries only runtime deps + the Aravis
# install, NOT the build toolchain (meson/cmake/-dev/git). A builder stage compiles Aravis 0.8.x
# (>= 0.8.32, for FLIR extended-chunk payloads) from source; the runtime stage copies just the result.
#
# BASE_IMAGE (build-arg), matched to the host platform by cam-up:
#   JP6 -> nvcr.io/nvidia/l4t-base:r36.2.0 (slim L4T base, NOT the ~14 GB l4t-jetpack -- we use no CUDA
#                                          SDK. r36.2.0 is the last published l4t-base tag; fine for any
#                                          r36.x host since nvv4l2* are CSV-injected from the host.)
#   JP7 -> ubuntu:24.04                    (NVENC reaches the container via CDI, not CUDA)
# NVIDIA nvv4l2*/nvvidconv are injected at runtime (CSV on JP6, CDI on JP7); the FFV1 software path
# needs neither. Verify on the Jetson: `gst-inspect-1.0 aravissrc` and
# `python3 -c "import gi; gi.require_version('Aravis','0.8')"`.
ARG BASE_IMAGE=nvcr.io/nvidia/l4t-base:r36.2.0
ARG ARAVIS_VERSION=0.8.34

# ---- builder: compile Aravis (+ the GStreamer plugin + GI typelib) from source ----
FROM ${BASE_IMAGE} AS builder
ENV DEBIAN_FRONTEND=noninteractive
ARG ARAVIS_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
      meson ninja-build pkg-config cmake gettext git ca-certificates wget xz-utils \
      libglib2.0-dev libxml2-dev libusb-1.0-0-dev \
      gobject-introspection libgirepository1.0-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*
RUN cd /tmp \
    && wget -q "https://github.com/AravisProject/aravis/releases/download/${ARAVIS_VERSION}/aravis-${ARAVIS_VERSION}.tar.xz" \
    && tar xf "aravis-${ARAVIS_VERSION}.tar.xz" \
    && cd "aravis-${ARAVIS_VERSION}" \
    && meson setup build --prefix=/usr/local \
         -Dgst-plugin=enabled -Dintrospection=enabled -Dviewer=disabled -Dusb=enabled \
    && ninja -C build \
    && DESTDIR=/opt/aravis ninja -C build install     # stage the install for a clean COPY

# ---- runtime: base + runtime deps + the Aravis install (no build toolchain) ----
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
# kmod: libnvtvmr runs `lsmod` during NVENC init, else nvv4l2h26{4,5}enc aborts. Runtime libs only.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-yaml python3-numpy kmod \
      gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      libxml2 libusb-1.0-0 \
    # 24.04's plugins-bad ships the GStreamer `onnx` element (-> onnxruntime), which we never use and
    # whose arm64 init misbehaves; strip the plugin + the heavy lib so the registry scan can't load it.
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/aravis/usr/local /usr/local
RUN ldconfig

# Make the locally-installed Aravis typelib + libs + gst plugin discoverable (Jetson = aarch64).
ENV GI_TYPELIB_PATH=/usr/local/lib/aarch64-linux-gnu/girepository-1.0:/usr/lib/aarch64-linux-gnu/girepository-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:/usr/local/lib \
    GST_PLUGIN_PATH=/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0

WORKDIR /app
# Only Python dep is PyYAML (apt python3-yaml above); gi/GStreamer/Aravis are system bindings.
COPY core-driver/ /app/

ENV CAM_CONFIG=/app/config/camera.yaml
# Sensor container: the supervisor spawns the core + any enabled in-image (process) plugins. ENTRYPOINT
# is python3, so run the core alone with `main.py -c ...`, and non-python tools need `--entrypoint`.
ENTRYPOINT ["python3"]
CMD ["supervisor.py", "-c", "config/camera.yaml"]
