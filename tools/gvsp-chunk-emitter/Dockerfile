# Aravis 0.8.36 patched to emit GVSP chunk data, built from source with introspection, alongside
# GStreamer + our core. Both the emitter (arv-fake-gv-camera-0.8) and the receiver (our pipeline) use
# THIS patched Aravis -> no version mismatch. Ubuntu 22.04 gives the Aravis-0.8 GI namespace our code
# requires. Multistage (like core-driver/Dockerfile): a builder patches + compiles Aravis; the runtime
# stage keeps only runtime deps + the staged install, NOT the meson/ninja/gcc/-dev toolchain.
#
# Build (from repo root):
#   docker build -f tools/gvsp-chunk-emitter/Dockerfile -t cam-chunks .
ARG BASE_IMAGE=ubuntu:22.04
ARG ARAVIS_TAG=0.8.36

# ---- builder: patch + compile Aravis (+ the GStreamer plugin + GI typelib) from source ----
FROM ${BASE_IMAGE} AS builder
ENV DEBIAN_FRONTEND=noninteractive
ARG ARAVIS_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates meson ninja-build gcc pkg-config gettext python3 \
      libglib2.0-dev libxml2-dev \
      gobject-introspection libgirepository1.0-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
    && rm -rf /var/lib/apt/lists/*
COPY tools/gvsp-chunk-emitter/apply_chunk_patch.py /tmp/apply_chunk_patch.py
RUN cd /tmp && git clone --depth 1 --branch ${ARAVIS_TAG} https://github.com/AravisProject/aravis aravis \
    && python3 /tmp/apply_chunk_patch.py /tmp/aravis \
    && cd /tmp/aravis \
    && meson setup build --prefix=/usr/local \
         -Dintrospection=enabled -Dgst-plugin=enabled -Dviewer=disabled \
         -Dusb=disabled -Ddocumentation=disabled -Dtests=false \
    && ninja -C build \
    && DESTDIR=/opt/aravis ninja -C build install     # stage the install for a clean COPY

# ---- runtime: base + runtime deps + the patched Aravis install (no build toolchain) ----
FROM ${BASE_IMAGE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-yaml \
      gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-libav \
      libglib2.0-0 libxml2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/aravis/usr/local /usr/local
RUN ldconfig

ENV GI_TYPELIB_PATH=/usr/local/lib/aarch64-linux-gnu/girepository-1.0:/usr/local/lib/x86_64-linux-gnu/girepository-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:/usr/local/lib/x86_64-linux-gnu:/usr/local/lib \
    GST_PLUGIN_PATH=/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/local/lib/x86_64-linux-gnu/gstreamer-1.0 \
    PATH=/usr/local/bin:$PATH

WORKDIR /app
COPY core-driver/ /app/
