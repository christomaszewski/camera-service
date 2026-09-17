#!/usr/bin/env bash
# Same hardware-free suite locally and in CI. Build the selected image first.
set -euo pipefail
cd "$(dirname "$0")/.."
IMG="${CAM_DEV_IMAGE:-cam-dev:dev}"
REPORTS="${CAM_TEST_REPORT_DIR:-$PWD/test-results}"
mkdir -p "$REPORTS"
REPORTS="$(cd "$REPORTS" && pwd)"
docker run --rm -v "$PWD:/repo:ro" -v "$REPORTS:/reports" -w /repo \
  -e CAM_EXPECT_GST="${CAM_EXPECT_GST:-}" "$IMG" bash tools/ci_pytest.sh
