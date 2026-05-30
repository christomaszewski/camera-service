# WebRTC consumer: reads the core's raw shm endpoint and serves frames to remote viewers
# via webrtcsink (gst-plugins-rs). webrtcsink isn't packaged, so it's built from source
# (cargo cinstall). Ubuntu 24.04 = GStreamer 1.24, matched to gst-plugins-rs 0.13.x.
#
# Build (from repo root):  docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .
# Layers are split so a late failure doesn't recompile everything.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ARG GST_RS_TAG=0.13.7

# Build + runtime deps (single stage for robustness). plugins-ugly = x264enc (H.264);
# plugins-good = vp8enc/vp9enc; libnice/libsrtp = the WebRTC transport.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl build-essential pkg-config cmake libssl-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
      libnice-dev libsrtp2-dev libx264-dev libvpx-dev libopus-dev \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      python3 python3-gi gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain + cargo-c
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH=/root/.cargo/bin:$PATH
RUN cargo install cargo-c

RUN git clone --depth 1 --branch ${GST_RS_TAG} \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /tmp/gst-rs

# Build just the webrtc plugin + the signalling server. LTO off + limited jobs to dodge
# the OOM-during-link that the full/LTO build hits in Docker.
ENV CARGO_PROFILE_RELEASE_LTO=off CARGO_BUILD_JOBS=2
RUN cd /tmp/gst-rs \
    && cargo cinstall -p gst-plugin-webrtc --prefix=/usr/local --libdir=/usr/local/lib \
    && cargo build --release --bin gst-webrtc-signalling-server \
    && install -m755 target/release/gst-webrtc-signalling-server /usr/local/bin/ \
    && rm -rf /tmp/gst-rs /root/.cargo/registry /root/.cargo/git

# webrtcbin (used internally by webrtcsink/webrtcsrc) needs the GStreamer libnice
# elements for ICE — a separate package from the libnice C library.
RUN apt-get update && apt-get install -y --no-install-recommends gstreamer1.0-nice \
    && rm -rf /var/lib/apt/lists/*

ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/x86_64-linux-gnu:/usr/local/lib/aarch64-linux-gnu

WORKDIR /app
COPY plugins/webrtc-bridge/ /app/
CMD ["bash", "run.sh"]
