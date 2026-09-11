#!/usr/bin/env bash
# Smoke-test the replay source (camera.type: replay): record a short run with a fake
# camera, replay the run through the full service, and assert the roundtrip is EXACT --
# identical sidecar CSV (same stamps, same frame ids, no drops) and bit-identical
# frames (lossless codecs) / byte-identical bitstream (stream-copy).
#
# Prereq:  docker build -f core-driver/Dockerfile.dev -t cam-dev .
# Run from the repo root:  ./core-driver/tools/replay_test.sh
set -euo pipefail
IMG="${CAM_DEV_IMAGE:-cam-dev}"

echo "########## RAW GRAY8: usb-fake/ffv1 run -> replay -> re-record, exact roundtrip ##########"
docker run --rm -v "$PWD/core-driver:/app" "$IMG" bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. record a short GRAY8/FFV1 run ==="
  python3 main.py -c config/usb-fake.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE"
  ORIG_CSV=$(ls /data/recordings/usbfake-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usbfake-*-00000.mkv)
  ROWS=$(($(wc -l < "$ORIG_CSV") - 1))
  [ "$ROWS" -gt 0 ] || { echo "FAIL: original run recorded no frames"; exit 1; }
  echo "original rows: $ROWS"

  echo "=== 2. replay the run through the service (finite -> exits 0 on its own) ==="
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  grep -q "replay source:" /tmp/replay.log || { echo "FAIL: replay source never started"; exit 1; }
  grep -q "playback finished" /tmp/replay.log || { echo "FAIL: no clean EOF finalize"; exit 1; }

  echo "=== 3. sidecar CSV must be IDENTICAL (same stamps + ids, zero drops) ==="
  RE_CSV=$(ls /data/recordings/rerec-*.csv) || { echo "FAIL: no re-recorded CSV"; exit 1; }
  RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs from the original"; exit 1; }
  echo "CSV identical ($ROWS rows)"

  echo "=== 4. decoded frames must be BIT-IDENTICAL (ffv1 lossless roundtrip) ==="
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/a.raw >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/b.raw >/dev/null 2>&1
  [ -s /tmp/a.raw ] || { echo "FAIL: original decode produced nothing"; exit 1; }
  cmp /tmp/a.raw /tmp/b.raw || { echo "FAIL: replayed frames are not bit-identical"; exit 1; }
  echo "frames bit-identical ($(stat -c %s /tmp/a.raw) bytes)"

  echo "=== 5. loop: cycles must stay strictly monotonic in the re-recorded pts_ns ==="
  # Frame ids repeat every cycle (it IS the same frame again), and the pipeline PTS memo is keyed
  # on the stamp too -- so cycle N+1 must never replay cycle N pts into the muxer. speed: 0 (as
  # fast as the pipeline drains) rides the blocking recording appsrcs: no frame may be dropped.
  rm -f /data/recordings/rerec-*
  # Signal the service, not timeout: newer coreutils reports 130 when its monitor receives
  # SIGINT even if the child handled it and finalized successfully. Keep the hang deadline.
  timeout 60 bash -c "echo \$\$ > /tmp/replay-loop.pid; exec python3 main.py -c config/replay-loop-test.yaml" >/tmp/loop.log 2>&1 &
  LOOP=$!; sleep 6; kill -INT "$(cat /tmp/replay-loop.pid)"
  wait "$LOOP" || { echo "FAIL: looped replay exited non-zero"; tail -20 /tmp/loop.log; exit 1; }
  grep -q "replay: loop -> cycle" /tmp/loop.log || { echo "FAIL: replay never looped"; exit 1; }
  LOOP_CSV=$(ls /data/recordings/rerec-*.csv) || { echo "FAIL: no looped CSV"; exit 1; }
  python3 - "$LOOP_CSV" "$ROWS" <<EOF
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
orig = int(sys.argv[2])
pts = [int(r["pts_ns"]) for r in rows]
assert len(rows) > orig, f"only {len(rows)} rows: less than one full cycle ({orig})"
bad = [i for i in range(1, len(pts)) if pts[i] <= pts[i - 1]]
assert not bad, f"pts_ns not strictly increasing at rows {bad[:5]}"
fids = [int(r["frame_id"]) for r in rows]
if len(fids) >= 2 * orig:
    assert fids[:orig] == fids[orig:2 * orig], "frame ids should repeat per cycle"
print(f"loop ok: {len(rows)} rows ({len(rows) / orig:.1f} cycles), pts_ns strictly increasing, no drops")
EOF
'

echo
echo "########## TWO SESSIONS: activate/deactivate twice -> ONE replay plays both in order -> re-record ##########"
docker run --rm -v "$PWD/core-driver:/app" "$IMG" bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. record TWO lifecycle sessions (boot active -> USR2 -> USR1 -> USR2) ==="
  python3 main.py -c config/usb-fake.yaml >/tmp/rec2.log 2>&1 &
  CORE=$!; sleep 3; kill -USR2 "$CORE"; sleep 2; kill -USR1 "$CORE"; sleep 3; kill -USR2 "$CORE"
  sleep 1; kill -INT "$CORE"; wait "$CORE"
  mapfile -t CSVS < <(ls /data/recordings/usbfake-*.csv | sort)
  [ "${#CSVS[@]}" -eq 2 ] || { echo "FAIL: expected 2 recorded sessions, got ${#CSVS[@]}"; ls /data/recordings; exit 1; }
  A_CSV=${CSVS[0]}; B_CSV=${CSVS[1]}
  A_ROWS=$(($(wc -l < "$A_CSV") - 1)); B_ROWS=$(($(wc -l < "$B_CSV") - 1))
  [ "$A_ROWS" -gt 0 ] && [ "$B_ROWS" -gt 0 ] || { echo "FAIL: a session recorded no frames ($A_ROWS/$B_ROWS)"; exit 1; }
  echo "sessions: A=$A_ROWS rows, B=$B_ROWS rows"

  echo "=== 2. one replay of the directory plays A then B (the recorded gap), re-recording ONE session ==="
  timeout 180 python3 main.py -c config/replay-test.yaml >/tmp/replay2.log 2>&1 \
    || { echo "FAIL: replay exited non-zero"; tail -30 /tmp/replay2.log; exit 1; }
  grep -q "2 session(s)" /tmp/replay2.log || { echo "FAIL: the replay did not see 2 sessions"; grep "replay source" /tmp/replay2.log; exit 1; }
  grep -q "session 1/2 done -> session 2" /tmp/replay2.log || { echo "FAIL: no session handover logged"; exit 1; }
  grep -q "playback finished" /tmp/replay2.log || { echo "FAIL: no clean EOF finalize"; exit 1; }

  echo "=== 3. the re-recorded sidecar is A followed by B: ids + stamps verbatim, nothing counted lost, provenance ==="
  RE_CSV=$(ls /data/recordings/rerec-*.csv) || { echo "FAIL: no re-recorded CSV"; exit 1; }
  RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  cut -d, -f1,3,4,6,7 "$A_CSV" > /tmp/ab.csv; tail -n +2 "$B_CSV" | cut -d, -f1,3,4,6,7 >> /tmp/ab.csv
  cut -d, -f1,3,4,6,7 "$RE_CSV" > /tmp/re.csv
  diff /tmp/ab.csv /tmp/re.csv || { echo "FAIL: the re-recorded rows are not A followed by B"; exit 1; }
  echo "CSV rows are A+B ($((A_ROWS + B_ROWS)) rows)"
  python3 - "${RE_CSV%.csv}.json" "${A_CSV%.csv}" "${B_CSV%.csv}" <<PYCHECK
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("replay_of") == [sys.argv[2], sys.argv[3]], d.get("replay_of")
drops = d.get("drops") or {}
assert drops.get("frames_missing", 0) == 0 and drops.get("source_gaps", 0) == 0, drops
print("provenance ok: replay_of lists both sessions in order; frames_missing=0 across the boundary")
PYCHECK

  echo "=== 4. decoded frames must be A frames then B frames, bit-identical ==="
  gst-launch-1.0 filesrc location="${A_CSV%.csv}-00000.mkv" ! matroskademux ! avdec_ffv1 ! filesink location=/tmp/a2.raw >/dev/null 2>&1
  gst-launch-1.0 filesrc location="${B_CSV%.csv}-00000.mkv" ! matroskademux ! avdec_ffv1 ! filesink location=/tmp/b2.raw >/dev/null 2>&1
  cat /tmp/a2.raw /tmp/b2.raw > /tmp/ab.raw
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! avdec_ffv1 ! filesink location=/tmp/re.raw >/dev/null 2>&1
  [ -s /tmp/ab.raw ] || { echo "FAIL: original decode produced nothing"; exit 1; }
  cmp /tmp/ab.raw /tmp/re.raw || { echo "FAIL: replayed frames are not A+B bit-identical"; exit 1; }
  echo "frames bit-identical across the session boundary ($(stat -c %s /tmp/ab.raw) bytes)"
'

echo
echo "########## RAW GRAY16 (thermal shape): 16-bit ffv1 run -> replay roundtrip ##########"
docker run --rm -v "$PWD/core-driver:/app" "$IMG" bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  sed "s/GRAY8/GRAY16_LE/; s/name_prefix: usbfake/name_prefix: usb16/" \
    config/usb-fake.yaml > /tmp/usb-fake-gray16.yaml
  python3 main.py -c /tmp/usb-fake-gray16.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE"
  ORIG_CSV=$(ls /data/recordings/usb16-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usb16-*-00000.mkv)
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  RE_CSV=$(ls /data/recordings/rerec-*.csv); RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs"; exit 1; }
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/a.raw >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! avdec_ffv1 ! \
    filesink location=/tmp/b.raw >/dev/null 2>&1
  [ -s /tmp/a.raw ] && cmp /tmp/a.raw /tmp/b.raw \
    || { echo "FAIL: 16-bit replay not bit-identical"; exit 1; }
  echo "GRAY16 roundtrip bit-identical"
'

echo
echo "########## STREAM-COPY MJPEG: usb-fake-mjpeg run -> replay -> stream-copy again ##########"
docker run --rm -v "$PWD/core-driver:/app" "$IMG" bash -c '
  set -e
  mkdir -p /data/recordings /tmp/cam
  echo "=== 1. record a short MJPEG (stream-copy) run ==="
  python3 main.py -c config/usb-fake-mjpeg.yaml >/tmp/rec.log 2>&1 &
  CORE=$!; sleep 4; kill -INT "$CORE"; wait "$CORE" 2>/dev/null || true
  ORIG_CSV=$(ls /data/recordings/usbmjpeg-*.csv) || { echo "FAIL: no original CSV"; exit 1; }
  ORIG_MKV=$(ls /data/recordings/usbmjpeg-*-00000.mkv)

  echo "=== 2. replay: must run the stream-copy dual path and re-record the SAME bytes ==="
  timeout 120 python3 main.py -c config/replay-test.yaml >/tmp/replay.log 2>&1 \
    || { echo "FAIL: replay run exited non-zero"; tail -30 /tmp/replay.log; exit 1; }
  grep -q "stream-copy" /tmp/replay.log || { echo "FAIL: replay did not stream-copy"; exit 1; }
  RE_CSV=$(ls /data/recordings/rerec-*.csv); RE_MKV=$(ls /data/recordings/rerec-*-00000.mkv)
  diff "$ORIG_CSV" "$RE_CSV" || { echo "FAIL: replay CSV differs"; exit 1; }

  echo "=== 3. demuxed JPEG bitstream must be byte-identical (no re-encode anywhere) ==="
  gst-launch-1.0 filesrc location="$ORIG_MKV" ! matroskademux ! \
    filesink location=/tmp/a.mjpg >/dev/null 2>&1
  gst-launch-1.0 filesrc location="$RE_MKV" ! matroskademux ! \
    filesink location=/tmp/b.mjpg >/dev/null 2>&1
  [ -s /tmp/a.mjpg ] || { echo "FAIL: original demux produced nothing"; exit 1; }
  cmp /tmp/a.mjpg /tmp/b.mjpg || { echo "FAIL: stream-copy replay bitstream differs"; exit 1; }
  echo "stream-copy bitstream byte-identical ($(stat -c %s /tmp/a.mjpg) bytes)"
'
echo "PASS: replay_test"
