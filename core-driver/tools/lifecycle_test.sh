#!/usr/bin/env bash
# Smoke-test the recording LIFECYCLE (no Jetson, no camera): the core boots INACTIVE -- streaming to
# consumers, recorder off -- and SIGUSR1 / SIGUSR2 open and finalize recording SESSIONS, each its own
# run (prefix + .mkv segments + .csv/.json sidecar).
#   1. inactive: the transport flows (shm probe) and NO recording exists
#   2. USR1 -> session 1 records; USR2 finalizes it: the mkv decodes to EOS, the CSV has rows, the JSON
#      attests the session, and splitmuxsink split on its THRESHOLD (2-4 segments for ~5 s at
#      segment_seconds: 2) -- not per keyframe, which is what a misread non-zero first PTS would do
#   3. USR1 -> session 2; SIGINT finalizes it: a SECOND prefix, session 1's files untouched, exit 0,
#      and the clean stop forgets the remembered state
#   4. a KILL while active keeps the remembered state and the restart RESUMES active unprompted
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/lifecycle_test.sh
set -euo pipefail

docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  R=/data/recordings
  start_core() { python3 main.py -c config/fake-camera-session.yaml >"$1" 2>&1 & CORE=$!; }
  seg_count() { ls "$R"/"$1"-*.mkv 2>/dev/null | wc -l; }
  rows() { echo $(( $(wc -l < "$1") - 1 )); }
  decode_ok() {
    gst-launch-1.0 filesrc location="$1" ! matroskademux ! avdec_ffv1 ! fakesink >/tmp/decode.log 2>&1 \
      || { echo "FAIL: $1 decode errored"; tail -5 /tmp/decode.log; exit 1; }
    grep -qi "got eos" /tmp/decode.log || { echo "FAIL: $1 decode never reached EOS"; exit 1; }
  }

  echo "=== 1. boot INACTIVE: transport flows, nothing recorded ==="
  start_core /tmp/core.log
  sleep 4
  grep -q "lifecycle: booting inactive (config)" /tmp/core.log \
    || { echo "FAIL: did not boot inactive"; tail -20 /tmp/core.log; exit 1; }
  python3 tools/shm_probe.py --socket /tmp/cam/frames --count 5 --timeout 5
  [ -z "$(ls "$R"/fake-*.mkv 2>/dev/null)" ] || { echo "FAIL: recorded while inactive"; ls -la "$R"; exit 1; }
  [ ! -e /tmp/cam/lifecycle.state ] || { echo "FAIL: state remembered before any transition"; exit 1; }
  echo "inactive: consumers fed, no recording, no remembered state"

  echo "=== 2. USR1 -> session 1 records; USR2 finalizes it ==="
  kill -USR1 "$CORE"; sleep 5
  python3 tools/shm_probe.py --socket /tmp/cam/frames --count 3 --timeout 5   # consumers flow while ACTIVE
  [ "$(cat /tmp/cam/lifecycle.state)" = active ] || { echo "FAIL: active not remembered"; exit 1; }
  kill -USR2 "$CORE"; sleep 2
  [ "$(cat /tmp/cam/lifecycle.state)" = inactive ] || { echo "FAIL: inactive not remembered"; exit 1; }
  P1=$(ls "$R"/fake-*.json | head -1); P1=${P1%.json}; P1=${P1##*/}
  echo "session 1 prefix: $P1"
  N1=$(seg_count "$P1")
  [ "$N1" -ge 2 ] && [ "$N1" -le 4 ] \
    || { echo "FAIL: expected 2-4 segments for ~5s at segment_seconds=2, got $N1 (split per keyframe?)"; ls "$R"; exit 1; }
  decode_ok "$R/$P1-00000.mkv"
  [ "$(rows "$R/$P1.csv")" -gt 0 ] || { echo "FAIL: session 1 CSV has no rows"; exit 1; }
  python3 - "$R/$P1.json" <<EOF
import json, sys
d = json.load(open(sys.argv[1]))
s = d["session"]
assert d["drops"]["frames"] > 0, d
assert s["frames_recorded"] > 0 and s["truncated"] is False and s["error"] is None, s
assert s["first_pts_ns"] > 1_000_000_000, "opened seconds in: the first PTS is the PROCESS timeline, not 0"
assert d["session_index"] == 1 and d["first_pts_ns"] == s["first_pts_ns"], d
print("session 1 JSON attests:", {k: s[k] for k in ("frames_recorded", "segments", "truncated")})
EOF
  grep -q "recording session 1 finalized" /tmp/core.log || { echo "FAIL: no finalize log for session 1"; exit 1; }

  echo "=== 3. USR1 -> session 2; SIGINT finalizes it (second prefix, first run intact) ==="
  SUM1=$(md5sum "$R/$P1.csv" "$R"/"$P1"-*.mkv)
  kill -USR1 "$CORE"; sleep 3
  kill -INT "$CORE"
  if wait "$CORE"; then echo "core exit: 0"; else echo "FAIL: core exit $?"; tail -20 /tmp/core.log; exit 1; fi
  P2=$(ls "$R"/fake-*.json | grep -v "$P1" | head -1); P2=${P2%.json}; P2=${P2##*/}
  [ -n "$P2" ] && [ "$P2" != "$P1" ] || { echo "FAIL: no second prefix"; ls "$R"; exit 1; }
  echo "session 2 prefix: $P2"
  decode_ok "$R/$P2-00000.mkv"
  [ "$(rows "$R/$P2.csv")" -gt 0 ] || { echo "FAIL: session 2 CSV has no rows"; exit 1; }
  [ "$(md5sum "$R/$P1.csv" "$R"/"$P1"-*.mkv)" = "$SUM1" ] || { echo "FAIL: session 1 files changed"; exit 1; }
  [ ! -e /tmp/cam/lifecycle.state ] || { echo "FAIL: a clean stop must forget the remembered state"; exit 1; }
  echo "two finalized runs, remembered state forgotten on the clean stop"

  echo "=== 4. KILL while ACTIVE -> the restart RESUMES active ==="
  start_core /tmp/core2.log
  sleep 3
  kill -USR1 "$CORE"; sleep 2
  kill -KILL "$CORE"; wait "$CORE" 2>/dev/null || true
  [ "$(cat /tmp/cam/lifecycle.state)" = active ] || { echo "FAIL: state not kept across a kill"; exit 1; }
  start_core /tmp/core3.log
  sleep 5
  grep -q "lifecycle: booting active (resumed)" /tmp/core3.log \
    || { echo "FAIL: restart did not resume active"; tail -20 /tmp/core3.log; exit 1; }
  N=$(ls "$R"/fake-*.json | wc -l)
  [ "$N" -ge 4 ] || { echo "FAIL: expected a 4th session prefix after the resume, have $N"; ls "$R"; exit 1; }
  kill -INT "$CORE"; wait "$CORE"
  echo "resumed session finalized; runs on disk:"; ls "$R"
'

echo
echo "########## zenoh control plane, peer-to-peer -- NO router anywhere (docs/LIFECYCLE.md) ##########"
docker run --rm -v "$PWD/core-driver:/app" cam-dev bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  export VEHICLE_ID=testveh CAM_INSTANCE=cam_fake
  R=/data/recordings/$CAM_INSTANCE          # CAM_INSTANCE also namespaces the recording dir (main.py)
  KEY=fleet/testveh/svc/cam_fake/lifecycle
  python3 main.py -c config/fake-camera-session.yaml >/tmp/core.log 2>&1 &
  CORE=$!
  echo "=== the probe LISTENS on the core default endpoint (tcp/localhost:7447), standing in for a router ==="
  # The core connects to it (its connector retries until it does); no zenohd exists in this container.
  python3 tools/lifecycle_probe.py --listen tcp/127.0.0.1:7447 --timeout 60 \
      --steps wait-put get:inactive bad activate wait-state:active sleep:5 deactivate wait-state:inactive get:inactive \
      >/tmp/probe.log 2>&1 || { echo "FAIL: probe steps"; cat /tmp/probe.log; tail -30 /tmp/core.log; exit 1; }
  grep -E "EVENT|DESCRIPTOR|REPLY|STATE|STEP|SUMMARY" /tmp/probe.log
  grep -q "EVENT PUT $KEY" /tmp/probe.log || { echo "FAIL: presence at the wrong key"; exit 1; }
  grep -q "REPLY activate ok=True state=active" /tmp/probe.log || { echo "FAIL: activate reply"; exit 1; }
  grep -q "REPLY reboot ok=False" /tmp/probe.log || { echo "FAIL: a bad transition must be a refusal reply"; exit 1; }
  grep -q "REPLY deactivate ok=True state=inactive" /tmp/probe.log || { echo "FAIL: deactivate reply"; exit 1; }
  grep -q "STATE $KEY/state active" /tmp/probe.log || { echo "FAIL: no state publication observed"; exit 1; }
  P1=$(ls "$R"/fake-*.json | head -1); P1=${P1%.json}; P1=${P1##*/}
  [ -n "$P1" ] || { echo "FAIL: no recording from the zenoh-activated session"; ls "$R"; exit 1; }
  python3 - "$R/$P1.json" <<EOF
import json, sys
d = json.load(open(sys.argv[1]))
s = d["session"]
assert s["frames_recorded"] > 0 and s["truncated"] is False and s["error"] is None, s
print("zenoh-driven session attests:", {k: s[k] for k in ("frames_recorded", "segments", "truncated")})
EOF
  [ "$(cat /tmp/cam/lifecycle.state)" = inactive ] || { echo "FAIL: state file after deactivate"; exit 1; }
  grep -q "control plane: activate -> ok=True" /tmp/core.log || { echo "FAIL: core did not log the zenoh transition"; exit 1; }

  echo "=== presence DELETE on a graceful stop (a second probe reconnects, then the core stops) ==="
  python3 tools/lifecycle_probe.py --listen tcp/127.0.0.1:7447 --timeout 60 --steps wait-put wait-delete \
      >/tmp/probe2.log 2>&1 &
  PROBE=$!
  for _ in $(seq 1 30); do grep -q "EVENT PUT" /tmp/probe2.log && break; sleep 1; done
  grep -q "EVENT PUT" /tmp/probe2.log || { echo "FAIL: the second probe never saw the token"; cat /tmp/probe2.log; exit 1; }
  kill -INT "$CORE"; wait "$CORE"
  wait "$PROBE" || { echo "FAIL: DELETE not observed"; cat /tmp/probe2.log; exit 1; }
  grep -E "EVENT|STEP|SUMMARY" /tmp/probe2.log
  echo "zenoh control plane: presence, descriptor, change_state, state publications, DELETE on stop -- no router"
'
echo "PASS: lifecycle_test"
