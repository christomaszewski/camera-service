#!/usr/bin/env bash
# Full input->output round-trip over GVSP against known ground truth: verifies lossless
# frames (bit-exact vs the EXACT bytes transmitted) AND timestamp fidelity (injected
# ChunkTimestamp == logged chunk_ns). No Jetson, no camera.
#
# Prereq:  docker build -f tools/gvsp-chunk-emitter/Dockerfile -t cam-chunks .
# Run (random-noise frames):   ./tools/gvsp-chunk-emitter/roundtrip_test.sh
# Run with a real input file:  ./tools/gvsp-chunk-emitter/roundtrip_test.sh path/to/video.mkv
#   (any gst-decodable video/image; it's decoded + scaled to GRAY8 512x512 and cycled to N)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

MOUNT=(); ARGS=()
INPUT="${1:-}"
if [ -n "$INPUT" ]; then
  [ -f "$INPUT" ] || { echo "input file not found: $INPUT" >&2; exit 1; }
  ABS="$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")"
  MOUNT=(-v "$ABS:/input/source:ro")
  ARGS=(--input /input/source)
fi

# NB: ${arr[@]+"${arr[@]}"} guards empty-array expansion under `set -u` on bash 3.2 (macOS).
docker run --rm -v "$REPO/core-driver:/app" -v "$REPO/tools/gvsp-chunk-emitter:/t" \
  ${MOUNT[@]+"${MOUNT[@]}"} \
  cam-chunks python3 /t/roundtrip_test.py ${ARGS[@]+"${ARGS[@]}"}
