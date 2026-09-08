# Core GigE Vision driver — multistage so the shipped image carries only runtime deps + the Aravis
# install, NOT the build toolchain (meson/cmake/-dev/git). A builder stage compiles Aravis 0.8.x
# (>= 0.8.32, for FLIR extended-chunk payloads) from source; the runtime stage copies just the result.
#
# BASE_IMAGE (build-arg), matched to the host platform by cam-up:
#   JP6 -> nvcr.io/nvidia/l4t-base:r36.2.0 (slim L4T base, NOT the ~14 GB l4t-jetpack -- we use no CUDA
#                                          SDK. r36.2.0 is the last published l4t-base tag; fine for any
#                                          r36.x host since nvv4l2* are CSV-injected from the host.)
#   JP7 -> ubuntu:24.04                    (NVENC reaches the container via CDI, not CUDA)
#   jp6m -> ubuntu:26.04                   (EXPERIMENT, docs/jp6-modern-userspace.md: a modern userspace --
#                                          GStreamer 1.28 -- on a JP6 host, the L4T multimedia stack injected
#                                          by the host's nvidia runtime (CSV) or a csv-mode CDI spec)
# NVIDIA nvv4l2*/nvvidconv are injected at runtime (CSV on JP6, CDI on JP7); the FFV1 software path
# needs neither. Verify on the Jetson: `gst-inspect-1.0 aravissrc` and
# `python3 -c "import gi; gi.require_version('Aravis','0.8')"`.
ARG BASE_IMAGE=nvcr.io/nvidia/l4t-base:r36.2.0
ARG ARAVIS_VERSION=0.8.34
ARG L4T_MULTIMEDIA=
ARG L4T_VERSION=

# ---- l4t: JetPack 6's multimedia + GStreamer plugin layer, EXTRACTED from NVIDIA's apt repo ----
# On a JP6 host the nvidia runtime injects drivers + device nodes only (drivers.csv / devices.csv);
# the multimedia libraries (NvBufSurface, libnvv4l2 + the v4l2 codec plugin, libnvtvmr ...) and the
# nvv4l2* / nvvidconv GStreamer plugins are the CONTAINER's to carry -- NVIDIA's l4t-jetpack image
# installs them from the same repo. `dpkg-deb -x`, no maintainer scripts (nvidia-l4t-core's would
# refuse a non-Jetson build host). L4T_MULTIMEDIA names the release line (r36.4); L4T_VERSION pins
# the package version to the host's L4T (36.4.4-... for an R36.4.4 host); empty = no layer (JP7's
# CDI injects the whole stack; dev has none). docs/jp6-modern-userspace.md "Finding on JP6".
FROM ubuntu:22.04 AS l4t
ARG L4T_MULTIMEDIA
ARG L4T_VERSION
RUN mkdir -p /l4t/usr/lib/aarch64-linux-gnu \
    && if [ -n "$L4T_MULTIMEDIA" ]; then \
         apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
         && curl -fsSL https://repo.download.nvidia.com/jetson/jetson-ota-public.asc -o /etc/apt/trusted.gpg.d/jetson.asc \
         && printf 'deb https://repo.download.nvidia.com/jetson/common %s main\ndeb https://repo.download.nvidia.com/jetson/t234 %s main\n' "$L4T_MULTIMEDIA" "$L4T_MULTIMEDIA" > /etc/apt/sources.list.d/nvidia-l4t.list \
         && apt-get update && cd /tmp \
         && for p in nvidia-l4t-core nvidia-l4t-nvsci nvidia-l4t-multimedia-utils nvidia-l4t-multimedia nvidia-l4t-gstreamer; do \
              apt-get download "$p${L4T_VERSION:+=$L4T_VERSION}" && dpkg-deb -x "$p"_*.deb /l4t; \
            done \
         # the plugins the stack uses; the camera / display ones would only fail the registry scan here
         && cd /l4t/usr/lib/aarch64-linux-gnu/gstreamer-1.0 \
         && ls | grep -vE '^libgst(nvvidconv|nvvideo4linux2|nvjpeg|nvunixfd|nvtee|nvcompositor)\.so$' | xargs -r rm -f \
         && rm -rf /l4t/usr/share /l4t/etc /l4t/usr/bin /l4t/opt /l4t/usr/src /l4t/usr/include /l4t/var; \
       fi

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
ARG L4T_MULTIMEDIA
ENV DEBIAN_FRONTEND=noninteractive
# kmod: libnvtvmr runs `lsmod` during NVENC init, else nvv4l2h26{4,5}enc aborts. Runtime libs only.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-yaml python3-numpy python3-pip kmod \
      gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      libusb-1.0-0 \
      # Aravis' runtime libxml2: `libxml2` up to 24.04, `libxml2-16` on 26.04 -- whichever this base has
      $(apt-cache policy libxml2 | grep -q 'Candidate: [0-9]' && echo libxml2 || echo libxml2-16) \
    # 24.04's plugins-bad ships the GStreamer `onnx` element (-> onnxruntime), which we never use and
    # whose arm64 init misbehaves; strip the plugin + the heavy lib so the registry scan can't load it.
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/aravis/usr/local /usr/local
# JP6's multimedia layer (empty unless L4T_MULTIMEDIA): nvidia/*.so, the six nv GStreamer plugins, the
# v4l2 codec plugin, and libv4l2.so.0.0.999999 -> nvidia/libnvv4l2.so (NVIDIA's libv4l2 replaces the
# distro's through the version trick; the ldconfig below picks it). Files the runtime injects on top
# (drivers.csv) shadow the same paths -- the host's driver build always wins where it is mounted.
COPY --from=l4t /l4t/usr/lib/aarch64-linux-gnu/ /usr/lib/aarch64-linux-gnu/
# The L4T libraries the host injects at runtime (CSV on JP6, CDI on JP7) land in
# /usr/lib/aarch64-linux-gnu/nvidia (r36) or tegra{,-egl} (r35). An l4t-base image already lists those dirs for the
# linker; a plain Ubuntu base does not, and then libgstnvvideo4linux2.so cannot resolve
# libnvbufsurface & co. after the mount (docs/jp6-modern-userspace.md). Listing them is harmless
# where nothing is mounted. The runtime's post-mount ldconfig refreshes the cache from this file.
RUN printf '/usr/lib/aarch64-linux-gnu/nvidia\n/usr/lib/aarch64-linux-gnu/tegra\n/usr/lib/aarch64-linux-gnu/tegra-egl\n' > /etc/ld.so.conf.d/nvidia-tegra.conf \
    && ldconfig

# eclipse-zenoh: the Python Zenoh binding for the lifecycle control plane (docs/LIFECYCLE.md). A prebuilt
# manylinux abi3 wheel installs on arm64/x86 -- no Rust build. The SAME pin as the webrtc-bridge, so one
# zenoh minor rides the vehicle (with rmw_zenoh's on the ROS graph). PIP_BREAK_SYSTEM_PACKAGES: the
# JP7 base (24.04) is PEP 668 externally-managed; the JP6 base (22.04) pip ignores the variable.
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN pip3 install --no-cache-dir eclipse-zenoh==1.9.0

# Make the locally-installed Aravis typelib + libs + gst plugin discoverable (Jetson = aarch64).
ENV GI_TYPELIB_PATH=/usr/local/lib/aarch64-linux-gnu/girepository-1.0:/usr/lib/aarch64-linux-gnu/girepository-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:/usr/local/lib:/usr/lib/aarch64-linux-gnu/nvidia:/usr/lib/aarch64-linux-gnu/tegra:/usr/lib/aarch64-linux-gnu/tegra-egl \
    GST_PLUGIN_PATH=/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0

WORKDIR /app
# Python deps: PyYAML (apt python3-yaml) + eclipse-zenoh (pip, above); gi/GStreamer/Aravis are system bindings.
COPY core-driver/ /app/

ENV CAM_CONFIG=/app/config/camera.yaml
# Sensor container: the supervisor spawns the core + any enabled in-image (process) plugins. ENTRYPOINT
# is python3, so run the core alone with `main.py -c ...`, and non-python tools need `--entrypoint`.
ENTRYPOINT ["python3"]
CMD ["supervisor.py", "-c", "config/camera.yaml"]
