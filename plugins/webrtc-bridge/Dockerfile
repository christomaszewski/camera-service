# WebRTC consumer: reads the core's raw shm endpoint and serves frames to remote viewers via
# webrtcsink (gst-plugins-rs, built from source). Multistage: the builder carries the whole Rust
# toolchain + cargo-c to compile the plugin; the runtime stage keeps ONLY the built plugin + the
# signalling binary + runtime GStreamer -- dropping ~1.5 GB of Rust/-dev. Ubuntu 24.04 = GStreamer
# 1.24, matched to gst-plugins-rs 0.13.x.
#
# Build (from repo root):  docker build -f plugins/webrtc-bridge/Dockerfile -t webrtc-bridge .
# The GStreamer <-> gst-plugins-rs pairing follows the base: ubuntu:24.04 = GStreamer 1.24 <-> 0.13.x
# (the default, validated on JP7); ubuntu:26.04 = GStreamer 1.28 <-> 0.15.x (the jp6m experiment,
# docs/jp6-modern-userspace.md: `--build-arg BASE_IMAGE=ubuntu:26.04 --build-arg GST_RS_TAG=0.15.3`).
ARG BASE_IMAGE=ubuntu:24.04
ARG GST_RS_TAG=0.13.7
# JP6 hosts: bake the multimedia + nv GStreamer plugin layer (see core-driver/Dockerfile's l4t stage --
# the same one, so webrtcsink can rank nvv4l2h264enc in this container too).
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

# Pinned Rust toolchain + cargo-c (+ --locked): unpinned, this build broke in the field with NO repo
# change (2026-06-10, fresh JP6 host): rustup grabbed the just-released stable and an UNLOCKED
# `cargo install cargo-c` resolved a fresh dep set whose `time` crate fails trait coherence inside
# cargo-credential ("conflicting implementations of trait `From<...HourBase>`") -- while hosts with
# cached builder layers kept working on the frozen old toolchain. --locked builds cargo-c with the
# dep set it shipped with; the pins make the layer deterministic. Bump the PAIR together, deliberately.
# (Verified on arm64: rustc 1.96.0 + cargo-c 0.10.23 --locked build clean; gst-plugin-webrtc 0.13.7's
# MSRV is 1.71, so the toolchain pin has wide headroom. gst-plugins-rs itself ships a Cargo.lock, so
# its dep set was already reproducible -- the install steps here were the only float.)
ARG RUST_TOOLCHAIN=1.96.0
ARG CARGO_C_VER=0.10.23

# ---- builder: compile gst-plugin-webrtc + the signalling server (Rust) ----
FROM ${BASE_IMAGE} AS builder
ENV DEBIAN_FRONTEND=noninteractive
ARG GST_RS_TAG
ARG RUST_TOOLCHAIN
ARG CARGO_C_VER
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl build-essential pkg-config cmake libssl-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev \
      libnice-dev libsrtp2-dev libx264-dev libvpx-dev libopus-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
      --default-toolchain ${RUST_TOOLCHAIN}
ENV PATH=/root/.cargo/bin:$PATH
RUN cargo install cargo-c --version ${CARGO_C_VER} --locked
RUN git clone --depth 1 --branch ${GST_RS_TAG} \
      https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /tmp/gst-rs
# LTO off + limited jobs to dodge the OOM-during-link the full/LTO build hits in Docker.
ENV CARGO_PROFILE_RELEASE_LTO=off CARGO_BUILD_JOBS=2
# gst-plugin-rtp rides along for `rtpgccbwe`: webrtcsink's Google Congestion Control needs that
# element to scale each consumer's encoder bitrate to the link. Without it CC never engages --
# every consumer parks at start-bitrate AND a pinned rate above a (WiFi) link's momentary capacity
# bufferbloats the stream (latency grows without recovering). Same RUN -> shared cargo target dir,
# so the common gstreamer-rs deps compile once.
RUN cd /tmp/gst-rs \
    && cargo cinstall -p gst-plugin-webrtc --prefix=/usr/local --libdir=/usr/local/lib \
    && cargo cinstall -p gst-plugin-rtp --prefix=/usr/local --libdir=/usr/local/lib \
    && cargo build --release --bin gst-webrtc-signalling-server \
    && install -m755 target/release/gst-webrtc-signalling-server /usr/local/bin/

# ---- runtime: GStreamer + libnice ICE elements + the built plugin/binary (no Rust, no -dev) ----
FROM ${BASE_IMAGE}
ARG L4T_MULTIMEDIA
ENV DEBIAN_FRONTEND=noninteractive
# Runtime GStreamer + the libnice ICE elements; the codec/crypto runtime libs (x264/vpx/opus/srtp/ssl)
# arrive as plugin dependencies. gstreamer1.0-nice = the ICE elements webrtcbin needs (separate from
# the libnice C lib). onnx element stripped as elsewhere. ca-certificates: webrtcsink's signaller
# builds an OpenSSL TLS connector at startup (to support wss://) -- without a CA store that build
# fails with "Signalling error: ... OpenSSL error" even on a plain ws:// URI. kmod: the NVENC
# plugins are runtime-injected on Jetson (CSV on JP6, CDI on JP7) and their init runs `lsmod`
# (libnvtvmr) -- without kmod that aborts on r38 (warn-only noise on r39); same bake as the core.
# gstreamer1.0-x: despite the name, this is where Ubuntu packages the PANGO text elements
# (textoverlay -- the burned-in latency overlay); they are NOT in gstreamer1.0-plugins-base.
# fontconfig + fonts-dejavu-core: pango renders nothing without a resolvable font on this slim base.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-nice \
      gstreamer1.0-x ca-certificates kmod fontconfig fonts-dejavu-core \
      python3 python3-gi python3-pip python3-numpy gir1.2-glib-2.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
    && rm -f /usr/lib/*/gstreamer-1.0/libgstonnx.so /usr/lib/*/libonnxruntime.so* \
    && rm -rf /var/lib/apt/lists/*
# eclipse-zenoh: the Python Zenoh binding for the fleet-discovery advertiser (presence + descriptor;
# docs/DISCOVERY.md). A prebuilt manylinux abi3 wheel installs on arm64/x86 -- no Rust build. PEP668
# externally-managed env in-container -> --break-system-packages.
RUN pip3 install --break-system-packages --no-cache-dir eclipse-zenoh==1.9.0
COPY --from=builder /usr/local /usr/local
COPY --from=l4t /l4t/usr/lib/aarch64-linux-gnu/ /usr/lib/aarch64-linux-gnu/
# Same as the core: the runtime-injected L4T libs live in the tegra dirs; list them for the linker so
# the CSV/CDI-mounted nvv4l2* plugins resolve inside a plain Ubuntu base (HW encode in webrtcsink).
RUN printf '/usr/lib/aarch64-linux-gnu/nvidia\n/usr/lib/aarch64-linux-gnu/tegra\n/usr/lib/aarch64-linux-gnu/tegra-egl\n' > /etc/ld.so.conf.d/nvidia-tegra.conf \
    && ldconfig
ENV GST_PLUGIN_PATH=/usr/local/lib/gstreamer-1.0 \
    LD_LIBRARY_PATH=/usr/local/lib:/usr/local/lib/x86_64-linux-gnu:/usr/local/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu/nvidia:/usr/lib/aarch64-linux-gnu/tegra:/usr/lib/aarch64-linux-gnu/tegra-egl

WORKDIR /app
COPY plugins/webrtc-bridge/ /app/
CMD ["bash", "run.sh"]
