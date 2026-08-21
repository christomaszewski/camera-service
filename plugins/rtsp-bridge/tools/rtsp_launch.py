"""Pure-python builders for the rtsp-bridge factory launch string (no GStreamer import).

run.sh supplies the source half (CAM_SRC_CHAIN, ending in video/x-raw,format=I420 -- possibly as a
split appsink/appsrc pair for the frame pump); this module supplies the encode half and glues the
two into the string GstRTSPMediaFactory.set_launch() consumes. Kept gi-free so test_rtsp_launch.py
runs anywhere (same pattern as h264_level.py in the webrtc-bridge).
"""

# codec -> the element set per path. NVENC needs the platform grant (CDI on JP7, nvidia-runtime CSV
# on JP6) to exist in the container at all; the CPU encoders ship in the image (x264enc in
# plugins-ugly, x265enc in plugins-bad).
CODECS = {
    "h264": {"nv": "nvv4l2h264enc", "cpu": "x264enc",
             "parse": "h264parse", "pay": "rtph264pay"},
    "h265": {"nv": "nvv4l2h265enc", "cpu": "x265enc",
             "parse": "h265parse", "pay": "rtph265pay"},
}

# GstRTSPLowerTrans flag nicks factory.set_protocols understands (mapped to gi flags in rtsp_server).
PROTOCOLS = ("udp", "udp-mcast", "tcp")


def normalize_codec(spec):
    """CAM_RTSP_CODEC -> a CODECS key. Unknown values raise (the caller warns + falls back)."""
    c = (spec or "h264").strip().lower()
    if c not in CODECS:
        raise ValueError("unknown codec {!r} (want one of {})".format(spec, sorted(CODECS)))
    return c


def pick_encoder(codec, pinned, have):
    """Encoder element for `codec`: an explicit pin (CAM_RTSP_ENCODER != auto) wins verbatim; else
    the codec's NVENC element when `have(name)` says it exists, else the CPU encoder. `have` is a
    callable (Gst.ElementFactory.find at runtime) so this stays testable without GStreamer."""
    t = CODECS[codec]
    if pinned and pinned.strip().lower() not in ("", "auto"):
        return pinned.strip()
    return t["nv"] if have(t["nv"]) else t["cpu"]


def encoder_block(codec, encoder, bitrate_bps, keyint):
    """The encoder chain fragment for the launch string. bitrate_bps is bit/sec everywhere in env;
    x264enc/x265enc take kbit/sec, the nvv4l2 encoders take bit/sec. Only properties known to exist
    on the element go in the string (a bad property is a parse-time hard failure); anything
    version-dependent is set defensively at media-configure time instead (rtsp_server.setp)."""
    t = CODECS[codec]
    kbps = max(1, int(bitrate_bps) // 1000)
    keyint = max(1, int(keyint))
    if encoder == t["nv"]:
        # We own the NVMM hop (webrtcsink inserted it for us; a bare launch string doesn't).
        return ("nvvidconv ! video/x-raw(memory:NVMM),format=I420 ! "
                "{enc} name=cam_enc bitrate={bps} iframeinterval={ki}").format(
                    enc=encoder, bps=int(bitrate_bps), ki=keyint)
    if encoder == "x264enc":
        return ("x264enc name=cam_enc tune=zerolatency speed-preset=ultrafast "
                "bitrate={kbps} key-int-max={ki} bframes=0 b-adapt=false").format(
                    kbps=kbps, ki=keyint)
    if encoder == "x265enc":
        # x265 has no bframes property; option-string reaches the raw x265 param API.
        return ("x265enc name=cam_enc tune=zerolatency speed-preset=ultrafast "
                "bitrate={kbps} key-int-max={ki} option-string=\"bframes=0\"").format(
                    kbps=kbps, ki=keyint)
    # A user-pinned element we don't know: no properties in the string (any wrong name would kill the
    # parse); rtsp_server's defensive media-configure pass still probes bitrate/keyint-ish props.
    return "{enc} name=cam_enc".format(enc=encoder)


def build_factory_launch(src_chain, codec, enc_block):
    """The full GstRTSPMediaFactory launch description. The ( ) wrapping makes it a bin description;
    `pay0` is the factory's REQUIRED payloader name; pay config-interval=-1 re-sends the parameter
    sets (SPS/PPS, +VPS for H.265) with every IDR so mid-stream RTSP joiners can sync."""
    t = CODECS[codec]
    return "( {src} ! {enc} ! {parse} ! {pay} name=pay0 pt=96 config-interval=-1 )".format(
        src=src_chain.strip(), enc=enc_block, parse=t["parse"], pay=t["pay"])


def parse_protocols(spec):
    """CAM_RTSP_PROTOCOLS ('tcp' / 'udp,tcp' / ...) -> a tuple of valid nicks, or None when unset
    (leave the server default: udp+udp-mcast+tcp). Junk raises (caller warns + leaves default)."""
    s = (spec or "").strip().lower()
    if not s:
        return None
    toks = tuple(t.strip() for t in s.split(",") if t.strip())
    bad = [t for t in toks if t not in PROTOCOLS]
    if bad or not toks:
        raise ValueError("bad protocols {!r} (want a comma list of {})".format(spec, list(PROTOCOLS)))
    return toks


def normalize_mount_path(path, instance):
    """The RTSP mount path: CAM_RTSP_PATH verbatim (leading / enforced), default /<instance>."""
    p = (path or "").strip() or "/" + (instance or "camera")
    return p if p.startswith("/") else "/" + p


def rtsp_url(host, port, path):
    return "rtsp://{}:{}{}".format(host, int(port), path)
