#!/usr/bin/env bash
# Bit-exact validation of the HW HEVC-lossless recorder path on a JetPack 7 Orin (NVENC via CDI).
# Proves the GRAY8 -> NV24 -> NVMM -> nvv4l2h265enc(enable-lossless=1) -> decode round trip is
# byte-for-byte, i.e. truly lossless (not just a "lossless" codec over a range-scaled plane).
#
# Prereq on the host (once):  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# Build the core on a 24.04 base: docker build -f core-driver/Dockerfile -t gige-core \
#     --build-arg BASE_IMAGE=ubuntu:24.04 .
# Run:  ./core-driver/tools/nvenc_lossless_test.sh [--frames N]
#
# Self-contained: all artefacts live in the container's /tmp, so there's nothing to mount and
# no root-owned files left on the host. The image ENTRYPOINT is python3, so the script path is
# the argument (tools/nvenc_lossless_test.py resolves under the image WORKDIR /app).
set -u
exec docker run --rm --device nvidia.com/gpu=all --ipc=host \
  gige-core tools/nvenc_lossless_test.py "$@"
