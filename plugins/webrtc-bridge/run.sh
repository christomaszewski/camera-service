#!/usr/bin/env bash
# WebRTC producer: read the core's frames and serve them to remote viewers via webrtcsink (which
# encodes + does congestion control + multi-viewer fan-out itself). Also runs the gst-plugins-rs
# signalling server in-container, so viewers/consumers connect to <this-host>:${SIGNALLING_PORT}.
#
# Transport mirrors the ros2 bridge (must match the core), selected by CAM_PLATFORM:
#   JP7 -> unixfdsrc on the core's plugin_endpoint (/tmp/cam/unixfd). Self-describing caps:
#          geometry + the Bayer format come from the stream, so no config geometry is needed. Shares
#          the one socket with the ros2 bridge -- unixfdsink broadcasts to every connected client.
#   JP6 -> shmsrc on the raw endpoint (/tmp/cam/raw). Raw shm carries no caps, so geometry comes
#          from the sensor config (CAM_WIDTH/HEIGHT/FORMAT/FPS) and, for a CFA camera, the Bayer
#          pattern (CAM_BAYER) is applied as video/x-bayer caps. Needs transport.raw_endpoint.enabled.
#
# Color: a Bayer camera (CAM_BAYER set) is debayered to color in-pipeline (bayer2rgb), so the browser
# preview is RGB rather than a grayscale mosaic. Set CAM_WEBRTC_DEBAYER=false to preview the raw
# mosaic instead. Mono cameras are a straight passthrough (the appsink/encoder read the format off caps).
#
# Env (all optional): CAM_PLATFORM ({jp6|jp7}), CAM_TRANSPORT ({unixfd|shm} override),
# CAM_TRANSPORT_SOCKET (unixfd), CAM_SHM_SOCKET (raw shm), CAM_BAYER, CAM_WEBRTC_DEBAYER,
# CAM_WIDTH/HEIGHT/FORMAT/FPS (JP6 raw shm only), SIGNALLING_PORT, VIDEO_CAPS (e.g. "video/x-h264"
# to pin the codec), RUN_SIGNALLING (1=start the bundled signalling server, default 1).
set -eu

PLATFORM="${CAM_PLATFORM:-jp6}"
TRANSPORT="${CAM_TRANSPORT:-}"
if [ -z "$TRANSPORT" ]; then
  [ "$PLATFORM" = jp7 ] && TRANSPORT=unixfd || TRANSPORT=shm
fi

W="${CAM_WIDTH:-512}"; H="${CAM_HEIGHT:-512}"; FMT="${CAM_FORMAT:-GRAY8}"; FPS="${CAM_FPS:-25}"
PORT="${SIGNALLING_PORT:-8443}"
VCAPS="${VIDEO_CAPS:-}"
BAYER="${CAM_BAYER:-}"

# Debayer to color for a CFA camera (CAM_BAYER set) unless explicitly disabled. bayer2rgb reads the
# pattern from the input caps (unixfd carries it; the JP6 capsfilter below sets it).
DEBAYER_EL=""
case "${CAM_WEBRTC_DEBAYER:-auto}" in
  0|false|no|off) : ;;
  *) [ -n "$BAYER" ] && DEBAYER_EL="bayer2rgb ! " ;;
esac

# Source chain (+ socket path) per transport.
if [ "$TRANSPORT" = unixfd ]; then
  SOCK="${CAM_TRANSPORT_SOCKET:-/tmp/cam/unixfd}"
  # Self-describing: caps (incl. video/x-bayer,<pattern> for CFA) come from the stream.
  SRC="unixfdsrc name=cam_src socket-path=${SOCK}"
else
  SOCK="${CAM_SHM_SOCKET:-/tmp/cam/raw}"
  if [ -n "$DEBAYER_EL" ]; then
    CAPS="video/x-bayer,format=${BAYER},width=${W},height=${H},framerate=${FPS}/1"
  else
    CAPS="video/x-raw,format=${FMT},width=${W},height=${H},framerate=${FPS}/1"
  fi
  # Raw shm carries no PTS -> do-timestamp on arrival (webrtcsink needs valid buffer timestamps to
  # payload RTP / run congestion control).
  SRC="shmsrc name=cam_src socket-path=${SOCK} is-live=true do-timestamp=true ! ${CAPS}"
fi

# The core publishes the socket asynchronously; depends_on doesn't wait for readiness. Give it a
# chance so we don't fail-and-restart on a cold start (both shm + unixfd create a socket file).
for _ in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 1; done

if [ "${RUN_SIGNALLING:-1}" = "1" ]; then
  gst-webrtc-signalling-server --host 0.0.0.0 --port "$PORT" &
  sleep 1
fi

SINK="webrtcsink name=cam_webrtcsink signaller::uri=ws://127.0.0.1:${PORT}"
[ -n "$VCAPS" ] && SINK="$SINK video-caps=${VCAPS}"

# Force I420 after videoconvert: webrtcsink's encoders want a YUV format, not GRAY8/RGBx. The leaky
# queue drops frames if the encoder/network falls behind (live preview: the newest frame wins).
PIPELINE="${SRC} ! queue leaky=downstream max-size-buffers=4 ! ${DEBAYER_EL}videoconvert ! video/x-raw,format=I420 ! ${SINK}"

echo "webrtc-bridge: ${TRANSPORT} ${SOCK}${BAYER:+ bayer=${BAYER}}${DEBAYER_EL:+ (debayer->color)} -> webrtcsink (signalling :${PORT})"

# Default launcher: a small Python process (tools/bridge_stream.py) that OWNS this pipeline and, once it
# is streaming, advertises the stream over Zenoh for fleet discovery (docs/DISCOVERY.md). It shares this
# process, so the liveliness token lives exactly as long as the bridge (crash/kill -> presence withdrawn).
# Escape hatch: CAM_LAUNCHER=gst-launch runs the bare pipeline with NO discovery (debugging / minimal).
if [ "${CAM_LAUNCHER:-python}" = "gst-launch" ]; then
  echo "webrtc-bridge: launcher=gst-launch (discovery off)"
  exec gst-launch-1.0 -e ${PIPELINE}
fi
export CAM_PIPELINE="$PIPELINE"
exec python3 -u tools/bridge_stream.py
