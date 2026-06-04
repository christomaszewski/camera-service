# WebRTC consumer: reads the core's raw shm endpoint and serves frames to remote viewers via
# webrtcsink (gst-plugins-rs, built from source). Multistage: the builder carries the whole Rust
# toolchain + cargo-c to compile the plugin; the runtime stage keeps ONLY the built plugin + the
# signalling binary + runtime GStreamer -- dropping ~1.5 GB of Rust/-dev. Ubuntu 24.04 = GStreamer
# 1.24, matched to gst-plugins-rs 0.13.x.
#
# Build (from repo root):  docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .
ARG GST_RS_TAG=0.13.7

# ---- builder: compile gst-plugin-webrtc + the signalling server (Rust) ----
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
ARG GST_RS_TAG
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl build-essential pkg-config cmake libssl-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
      libnice-dev libsrtp2-dev libx264-dev libvpx-dev libopus-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH
RUN cargo install cargo-c
RUN git clone --depth 1 --branch ${GST_RS_TAG} \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /tmp/gst-rs
# LTO off + limited jobs to dodge the OOM-during-link the full/LTO build hits in Docker.
ENV CARGO_PROFILE_RELEASE_LTO=off CARGO_BUILD_JOBS=2
RUN cd /tmp/gst-rs \
    && cargo cinstall -p gst-plugin-webrtc --prefix=/usr/local --libdir=/usr/local/lib \
    && cargo build --release --bin gst-webrtc-signalling-server \
    && install -m755 target/release/gst-webrtc-signalling-server /usr/local/bin/

# ---- runtime: GStreamer + libnice ICE elements + the built plugin/binary (no Rust, no -dev) ----
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
# Runtime GStreamer + the libnice ICE elements; the codec/crypto runtime libs (x264/vpx/opus/srtp/ssl)
# arrive as plugin dependencies. gstreamer1.0-nice = the ICE elements webrtcbin needs (separate from
# the libnice C lib). onnx element stripped as elsewhere. ca-certificates: webrtcsink's signaller
# builds an OpenSSL TLS connector at startup (to support wss://) -- without a CA store that build
# fails with "Signalling error: ... OpenSSL error" even on a plain ws:// URI.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-nice \
      ca-certificates \
      python3 python3-gi gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local /usr/local
RUN ldconfig
ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/x86_64-linux-gnu:/usr/local/lib/aarch64-linux-gnu

WORKDIR /app
COPY plugins/webrtc-bridge/ /app/
CMD ["bash", "run.sh"]
