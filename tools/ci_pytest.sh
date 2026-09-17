#!/usr/bin/env bash
# Run INSIDE cam-dev. Repository is read-only; dependencies/reports stay outside it.
set -euo pipefail
python3 -m pip install --target /tmp/ci-test-deps 'pytest>=8,<10' 'coverage>=7,<8' 'pytest-timeout>=2,<3'
export PYTHONPATH=/tmp/ci-test-deps PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=/reports/coverage.data
python3 - <<'PY'
import os
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
actual = '.'.join(map(str, Gst.version()[:3]))
expected = os.environ.get('CAM_EXPECT_GST', '')
print('Testing GStreamer', actual, flush=True)
assert not expected or actual == expected or actual.startswith(expected + '.'), (expected, actual)
PY
set +e
timeout 300 python3 -m coverage run -m pytest core-driver/tests plugins/webrtc-bridge/tools \
  -p no:cacheprovider -q -ra --tb=short --durations=10 --timeout=45 --timeout-method=thread \
  --junitxml=/reports/junit.xml 2>&1 | tee /reports/pytest.log
test_status=${PIPESTATUS[0]}
set -e
if [ -f /reports/coverage.data ]; then
  python3 -m coverage xml -o /reports/coverage.xml
  python3 -m coverage json -o /reports/coverage.json
  python3 -m coverage report > /reports/coverage.txt
  cat /reports/coverage.txt
fi
[ "$test_status" -eq 0 ] || exit "$test_status"
python3 - <<'PY'
import xml.etree.ElementTree as ET
suites = ET.parse('/reports/junit.xml').getroot().iter('testsuite')
skips = sum(int(s.get('skipped', 0)) for s in suites)
if skips:
    raise SystemExit(f'CI refuses {skips} skipped tests; check bindings and plugins')
PY
