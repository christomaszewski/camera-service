#!/usr/bin/env bash
# RTSP producer: read the core's PLUGIN endpoint and serve it as a standard RTSP stream any client
# (ffplay/VLC/OpenCV/QGC/another recorder) can play -- no WebRTC signalling. The server itself is
# tools/rtsp_server.py (GstRtspServer, ONE shared ON-DEMAND media: the pipeline -- including the
# socket source -- exists only while at least one client is connected).
#
# Transport mirrors the ros2 bridge (must match the core), selected by CAM_PLATFORM:
#   JP7 -> unixfdsrc on the core's plugin_endpoint (/tmp/cam/unixfd). Self-describing caps; shares
#          the one socket with the other bridges (unixfdsink broadcasts to every client).
#   JP6 -> shmsrc on the shm+HEADER plugin endpoint (/tmp/cam/frames, application/x-cam-frame):
#          every buffer is [36-byte header][pixels]. The frame pump (tools/frame_pump.py) strips
#          the header, re-derives caps from it, and restamps PTS from its PTP capture time --
#          unlike the webrtc-bridge's raw-shm fallback, timing and format survive JP6.
#          NB this endpoint honors transport.plugin_endpoint.max_rate_hz (the raw one does not).
#
# The pump also hosts the 16-bit operator view (CAM_RTSP_NORMALIZE, both platforms): GRAY16 ->
# percentile-stretched GRAY8 before encode (videoconvert alone keeps the TOP byte -- near-black for
# LSB-aligned radiometric thermal). Values: off (default) | auto | "lo:hi" percentiles.
#
# Env (all optional): CAM_PLATFORM ({jp6|jp7}), CAM_TRANSPORT ({unixfd|shm} override),
# CAM_TRANSPORT_SOCKET (unixfd), CAM_SHM_SOCKET (header shm), CAM_BAYER, CAM_RTSP_DEBAYER,
# CAM_RTSP_NORMALIZE, CAM_WIDTH/HEIGHT/FPS (JP6: header cross-check + framerate caps),
# CAM_RTSP_{PORT,PATH,CODEC,BITRATE,KEYINT,PROTOCOLS,ENCODER,LATENCY} -- see the README.
set -eu

PLATFORM="${CAM_PLATFORM:-jp6}"
TRANSPORT="${CAM_TRANSPORT:-}"
if [ -z "$TRANSPORT" ]; then
  [ "$PLATFORM" = jp7 ] && TRANSPORT=unixfd || TRANSPORT=shm
fi

BAYER="${CAM_BAYER:-}"

# Debayer to color for a CFA camera (CAM_BAYER set) unless explicitly disabled. On JP7 the unixfd
# caps carry the pattern; on JP6 the pump relabels the header's GRAY8 mosaic as video/x-bayer.
DEBAYER_EL=""
case "${CAM_RTSP_DEBAYER:-auto}" in
  0|false|no|off) : ;;
  *) [ -n "$BAYER" ] && DEBAYER_EL="bayer2rgb ! " ;;
esac

NORM="$(printf %s "${CAM_RTSP_NORMALIZE:-off}" | tr '[:upper:]' '[:lower:]')"
case "$NORM" in 0|false|no|off|"") NORM="" ;; esac
if [ -n "$NORM" ] && [ -n "$DEBAYER_EL" ]; then
  echo "rtsp-bridge: CAM_RTSP_NORMALIZE ignored (Bayer/debayer path is 8-bit color)" >&2
  NORM=""
fi

# The pump split (appsink pump_in -> python -> appsrc pump_out) applies on JP6 always (header
# strip) and on JP7 only for the 16->8 normalize. async=false on pump_in is LOAD-BEARING: without
# it the two chains form a circular preroll deadlock (the appsrc chain's preroll waits on the pump,
# which only runs once PLAYING, which waits on that preroll) -- see webrtc-bridge/run.sh for the
# full history. The leaky depth-2 queues are the standing-latency/drop-cushion tradeoff.
PUMP_SINK="appsink name=pump_in emit-signals=true max-buffers=2 drop=true sync=false async=false"
POST="appsrc name=pump_out is-live=true format=time ! queue leaky=downstream max-size-buffers=2 ! ${DEBAYER_EL}videoconvert ! video/x-raw,format=I420"

PUMP=0
if [ "$TRANSPORT" = unixfd ]; then
  SOCK="${CAM_TRANSPORT_SOCKET:-/tmp/cam/unixfd}"
  # Self-describing: caps (incl. video/x-bayer,<pattern> for CFA) come from the stream.
  SRC="unixfdsrc name=cam_src socket-path=${SOCK}"
  if [ -n "$NORM" ]; then
    PUMP=1
    SRC_CHAIN="${SRC} ! queue leaky=downstream max-size-buffers=2 ! ${PUMP_SINK}   ${POST}"
  else
    SRC_CHAIN="${SRC} ! queue leaky=downstream max-size-buffers=2 ! ${DEBAYER_EL}videoconvert ! video/x-raw,format=I420"
  fi
else
  SOCK="${CAM_SHM_SOCKET:-/tmp/cam/frames}"
  PUMP=1
  # do-timestamp provides the arrival-time PTS fallback; the pump prefers the header's capture time.
  SRC="shmsrc name=cam_src socket-path=${SOCK} is-live=true do-timestamp=true ! application/x-cam-frame"
  SRC_CHAIN="${SRC} ! queue leaky=downstream max-size-buffers=2 ! ${PUMP_SINK}   ${POST}"
fi

# The core publishes the socket asynchronously; depends_on doesn't wait for readiness. Give it a
# chance so we don't fail-and-restart on a cold start (both shm + unixfd create a socket file).
for _ in $(seq 1 60); do [ -S "$SOCK" ] && break; sleep 1; done

echo "rtsp-bridge: ${TRANSPORT} ${SOCK}${BAYER:+ bayer=${BAYER}}${DEBAYER_EL:+ (debayer->color)}${NORM:+ normalize=${NORM}} pump=${PUMP} -> rtsp :${CAM_RTSP_PORT:-8554}"

export CAM_SRC_CHAIN="$SRC_CHAIN"
export CAM_PUMP="$PUMP"
export CAM_TRANSPORT_RESOLVED="$TRANSPORT"
export CAM_RTSP_NORMALIZE_RESOLVED="$NORM"
export CAM_RTSP_DEBAYER_RESOLVED="$([ -n "$DEBAYER_EL" ] && echo 1 || echo 0)"
exec python3 -u tools/rtsp_server.py
