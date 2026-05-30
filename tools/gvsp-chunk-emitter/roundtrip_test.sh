#!/usr/bin/env bash
# Full input->output round-trip over GVSP against known ground truth: verifies lossless
# frames (bit-exact) AND timestamp fidelity (injected ChunkTimestamp == logged chunk_ns).
# No Jetson, no camera.
#
# Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t gige-chunks .
# Run:     ./tools/gvsp-chunk-emitter/roundtrip_test.sh
set -u
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
docker run --rm -v "$REPO/core-driver:/app" -v "$REPO/tools/gvsp-chunk-emitter:/t" gige-chunks \
  python3 /t/roundtrip_test.py
