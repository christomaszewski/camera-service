#!/usr/bin/env bash
# WebRTC producer: read the core's RAW shm endpoint and serve frames to remote viewers
# via webrtcsink (which encodes + does congestion control + multi-viewer fan-out itself).
# Also runs the gst-plugins-rs signalling server in-container, so viewers/consumers
# connect to <this-host>:${SIGNALLING_PORT}.
#
# Env (all optional): GIGE_SHM_SOCKET, GIGE_WIDTH/HEIGHT/FORMAT/FPS (must match the core's
# raw endpoint caps), SIGNALLING_PORT, VIDEO_CAPS (e.g. "video/x-h264" to pin the codec),
# RUN_SIGNALLING (1=start the bundled signalling server, default 1).
set -eu
SOCK="${GIGE_SHM_SOCKET:-/tmp/gige/raw}"
W="${GIGE_WIDTH:-512}"; H="${GIGE_HEIGHT:-512}"; FMT="${GIGE_FORMAT:-GRAY8}"; FPS="${GIGE_FPS:-25}"
PORT="${SIGNALLING_PORT:-8443}"
VCAPS="${VIDEO_CAPS:-}"

if [ "${RUN_SIGNALLING:-1}" = "1" ]; then
  gst-webrtc-signalling-server --host 0.0.0.0 --port "$PORT" &
  sleep 1
fi

SINK="webrtcsink signaller::uri=ws://127.0.0.1:${PORT}"
[ -n "$VCAPS" ] && SINK="$SINK video-caps=${VCAPS}"

echo "webrtc-bridge: ${SOCK} (${FMT} ${W}x${H}@${FPS}) -> webrtcsink (signalling :${PORT})"
# Force I420 after videoconvert: webrtcsink's encoders want a YUV format, not GRAY8
# (a mono camera's native format), so converting up front lets encoder discovery succeed.
# do-timestamp=true is essential: shm carries no PTS, and webrtcsink needs valid buffer
# timestamps to payload RTP / run congestion control. Stamp them on arrival (live source).
exec gst-launch-1.0 -e \
  shmsrc socket-path="$SOCK" is-live=true do-timestamp=true ! \
  "video/x-raw,format=${FMT},width=${W},height=${H},framerate=${FPS}/1" ! \
  queue leaky=downstream max-size-buffers=4 ! videoconvert ! video/x-raw,format=I420 ! \
  ${SINK}
