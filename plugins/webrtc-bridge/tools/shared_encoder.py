#!/usr/bin/env python3
"""One encoder for every viewer (CAM_WEBRTC_ENCODE=shared): the fragment and its policies.

webrtcsink encodes once PER SESSION. Three operators on one camera are three encoders, and so is one
operator plus two sessions that did not die. Handed an already-encoded stream it skips its encoders
and only parses and payloads, so the bridge can put ONE encoder in front of it. Measured on an Orin at
1080p30 with NVENC: three viewers cost 58% of a core this way against about 145% per-viewer.

What the mode gives up: there is one bitrate (CAM_WEBRTC_BITRATE) for everybody and it does not back
off when a link degrades -- CAM_WEBRTC_MIN/MAX/START_BITRATE and the sink's automatic downscaling have
nothing to act on. It suits viewers behind one link of known capacity; it is opt-in.

Two things webrtcsink needs from a pre-encoded stream, both found on the device:

  complete caps, once   h264parse announces caps when the encoder's arrive and AGAIN once it has parsed
        the SPS (profile and level if the encoder left them out, then chroma-format, bit depths,
        coded-picture-structure). webrtcsink refuses a second announcement ("Renegotiation is not
        supported") and the pipeline dies not-negotiated. So profile and level are pinned on a
        capsfilter right after the encoder (pin_caps; bridge_stream fills in the level from the real
        geometry) and the remaining fields are declared up front with capssetter: the second
        announcement is then identical to the first and never sent.
  keyframes on request  the sink asks upstream for a keyframe (GstForceKeyUnit) when a session connects
        and on every PLI. nvv4l2h26Xenc ignores the event, so a joining viewer waited for the next
        periodic IDR; it has a `force-IDR` action instead. And one join sends dozens of requests, each
        of which would reach every viewer: KeyframeLimiter lets one keyframe through per interval and
        defers the rest into it.

Pure functions plus a small CLI: run.sh calls `python3 tools/shared_encoder.py` to get the fragment for
this container's encoders (it prints the fragment, or explains on stderr and exits 3).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h264_level import LEVELS, level_for_bitrate  # noqa: E402

BITRATE_DEFAULT = 4_000_000
PROFILES = ("constrained-baseline", "main", "high")
_CODEC_OF = {"video/x-h264": "h264", "video/x-h265": "h265"}
# Fields h26Xparse adds to its caps only after parsing the stream headers. Declared up front (the
# pipelines here are 8-bit 4:2:0 progressive by construction: I420 or NVMM NV12 into the encoder).
_PARSED_FIELDS = ("coded-picture-structure=(string)frame,chroma-format=(string)4:2:0,"
                  "bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8")
QUEUE = "queue name=shared_enc_q leaky=downstream max-size-buffers=2"
NVENC_MIN_LEVEL = "2.1"         # the lowest H.264 level nvv4l2h264enc writes into a stream (measured, R39)


def wanted(value):
    """CAM_WEBRTC_ENCODE: `shared` turns the mode on; anything else is per-viewer."""
    return str(value or "").strip().lower() == "shared"


def codec_for(video_caps):
    """The codec VIDEO_CAPS pins, `h264` when it pins nothing, None for one this mode cannot share."""
    caps = str(video_caps or "").strip()
    if not caps:
        return "h264"
    return _CODEC_OF.get(caps.split(",", 1)[0].strip())


def parse_bitrate(value, start_bitrate=None):
    """bit/s for the shared encoder: CAM_WEBRTC_BITRATE, else CAM_WEBRTC_START_BITRATE, else 4 Mbit/s."""
    for v in (value, start_bitrate):
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return BITRATE_DEFAULT


def parse_profile(value):
    """H.264 profile for the shared encoder. Unlike the per-viewer mode -- where webrtcsink forces
    constrained-baseline whatever is asked -- any of PROFILES is honoured; the default stays
    constrained-baseline, which every browser decodes. `baseline` means that too: it is what WebRTC
    negotiates under that name and what x264enc emits for it."""
    p = str(value or "").strip().lower()
    return p if p in PROFILES else "constrained-baseline"


def encoder_profile(encoder, profile, x264_preset=None):
    """The profile to pin for `encoder`: the one asked for, where the encoder will really produce it.

    x264enc at its `ultrafast` preset uses none of the tools above constrained-baseline (no CABAC, no
    8x8 transform), so that is what its stream is whatever it is asked for -- and asked for `high` it
    still LABELS its caps `high`. The parser then reads the real profile from the SPS, and a second,
    different announcement is what webrtcsink refuses; that this goes well today rests on the parser
    quietly keeping the label. Pinning what the encoder will produce leaves nothing to rescue.
    `superfast` and slower produce main and high as asked (measured: profile-level-id 4d40.. / 6400..)."""
    asked = parse_profile(profile)
    if encoder == "x264enc" and str(x264_preset or "ultrafast").strip().lower() == "ultrafast":
        return "constrained-baseline"
    return asked


_RANK_USABLE = 64      # GST_RANK_MARGINAL: below it webrtcsink does not consider an encoder either


def select_encoder(codec, available):
    """The encoder element for `codec`: the Jetson hardware encoder when its pieces are there, else
    software for H.264, else None.

    `available` is {element name: rank} (or just the names, all of one rank). The ranks are honoured
    the way per-viewer encoding honours them, so GST_PLUGIN_FEATURE_RANK remains the one lever: an
    encoder ranked NONE is not used, and the hardware encoder ranked BELOW x264enc gives way to it --
    for a deployment that keeps NVENC for the recorder. By default it outranks x264enc (257 to 256 on
    L4T), and it is the only H.265 encoder shared."""
    ranks = available if isinstance(available, dict) else {name: 256 for name in available}
    usable = lambda name: ranks.get(name, 0) >= _RANK_USABLE        # noqa: E731
    hw = "nvv4l2h264enc" if codec == "h264" else "nvv4l2h265enc"
    hw_ok = usable(hw) and "nvvidconv" in ranks
    sw_ok = codec == "h264" and usable("x264enc")
    if hw_ok and not (sw_ok and ranks["x264enc"] > ranks[hw]):
        return hw
    return "x264enc" if sw_ok else None


def pin_caps(encoder, codec, profile=None, level=None):
    """Caps for the capsfilter after the encoder, so the parser's FIRST announcement is its last.
    nvv4l2h264enc leaves profile and level out of its caps and takes both from downstream: pin both
    (the level must be the one the geometry needs -- h264_level). x264enc announces its own level and
    takes only the profile. H.265 encoders announce everything themselves.

    The level to pin for nvv4l2h264enc is hardware_level()'s: the one it will really write. It then
    announces SOME whole-number levels with a trailing ".0": asked for level 3 it encodes level 3 and
    announces "3.0" (level 4 comes back as "4"). A filter pinned to "3" refused that, and every
    SD-sized preview -- 640x480, a 5 MP camera scaled to 640 -- died not-negotiated. So a whole-number
    level is pinned in both spellings, and level_caps() puts the canonical one back in front of the
    parser (which will announce "3" once it has read the SPS)."""
    base = f"video/x-{codec},stream-format=byte-stream,alignment=au"
    if codec != "h264":
        return base
    caps = base + f",profile={parse_profile(profile)}"          # the caller's encoder_profile()
    if encoder == "nvv4l2h264enc" and level:
        level = str(level)
        caps += ",level=(string)" + ("{ %s, %s.0 }" % (level, level) if level.isdigit() else level)
    return caps


def hardware_level(level, bitrate):
    """The level nvv4l2h264enc will WRITE into the stream when asked for `level` at `bitrate` bit/s.

    It is not always the one asked for, and the difference is fatal here: the encoder announces the
    level it was asked for, the parser then reads the real one from the SPS and announces again, and
    webrtcsink refuses the second announcement. Measured on an R39 Orin (asked -> written):

        320x266  at 0.5 Mbit/s   1.3 -> 2.1      it writes nothing below 2.1
        480x400  at   6 Mbit/s   2.1 -> 3        level 2.1 carries 4 Mbit/s
        640x534  at  20 Mbit/s   3   -> 3.2      level 3 carries 10, 3.1 carries 14
        1280x1070 at 30 Mbit/s   4   -> 4.1      level 4 carries 20

    i.e. the lowest level that is at least the one asked for, at least 2.1, and whose bitrate limit
    (h264_level's table) covers the configured bitrate. Asking for exactly that level makes the encoder
    write it and announce it, so nothing changes afterwards. Erring upwards is harmless (it writes what
    it is asked for when that is more than it needs); erring downwards is the failure above. A level
    the table does not know is returned as it is."""
    if level not in LEVELS:
        return level
    floor = level if LEVELS.index(level) >= LEVELS.index(NVENC_MIN_LEVEL) else NVENC_MIN_LEVEL
    return level_for_bitrate(floor, int(bitrate or 0) / 1000.0)


def level_caps(codec, level=None):
    """Caps for the capssetter between the hardware encoder's filter and the parser (shared_enc_level):
    the level in the spelling the parser itself will use, whatever the encoder wrote. Without a level
    it changes nothing."""
    return f"video/x-{codec}" + (f",level=(string){level}" if level else "")


def chain(codec, encoder, bitrate, x264_preset="ultrafast"):
    """The launch fragment from raw I420 to the encoded stream webrtcsink takes: a queue, the encoder
    (named shared_enc), the capsfilter bridge_stream completes at run time (shared_enc_caps), for the
    hardware H.264 encoder the capssetter that respells its level (shared_enc_level, see pin_caps),
    the parser and the capssetter that declares what the parser would otherwise add later.

    The queue gives the upload and the encode their own thread, as webrtcsink's per-viewer pipelines
    have: without it they run on the thread that debayers and converts, and a 5 MP Bayer stream came
    out at 20 fps of the camera's 24. Leaky, like the bridge's ingress queue: a live preview drops to
    the newest frame rather than fall behind. bridge_stream sets the
    low-latency properties and the keyframe interval: on the hardware encoders when the frame rate is
    known, on x264enc before it starts (it takes no properties afterwards)."""
    if encoder in ("nvv4l2h264enc", "nvv4l2h265enc"):
        head = ("nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
                f"{encoder} name=shared_enc bitrate={int(bitrate)}")
    elif encoder == "x264enc":
        head = (f"x264enc name=shared_enc bitrate={max(1, int(bitrate) // 1000)} tune=zerolatency "
                f"speed-preset={x264_preset or 'ultrafast'}")
    else:
        raise ValueError(f"no shared encoder for {encoder!r}")
    parser = "h264parse" if codec == "h264" else "h265parse"
    if encoder == "nvv4l2h264enc":
        parser = f'capssetter name=shared_enc_level join=true caps="{level_caps(codec)}" ! {parser}'
    return (f'{QUEUE} ! {head} ! capsfilter name=shared_enc_caps caps="{pin_caps(encoder, codec)}" ! '
            f'{parser} config-interval=-1 ! capssetter join=true caps="video/x-{codec},{_PARSED_FIELDS}"')


class KeyframeLimiter:
    """At most one forced keyframe per `min_interval_s`, and never a request lost: one that arrives too
    soon after the last keyframe is deferred to the end of the interval, and any further ones fold
    into that pending keyframe."""

    def __init__(self, min_interval_s=1.0):
        self.min_interval_s = float(min_interval_s)
        self._last = None
        self._pending = False
        self.requests = 0
        self.forced = 0

    def request(self, now):
        """A keyframe was asked for. Returns the delay in seconds after which to force one (0.0 = now),
        or None when a forced keyframe is already pending and will serve this request too."""
        self.requests += 1
        if self._pending:
            return None
        self._pending = True
        if self._last is None:
            return 0.0
        return max(0.0, self.min_interval_s - (now - self._last))

    def fired(self, now):
        """The pending keyframe was forced."""
        self._pending = False
        self._last = now
        self.forced += 1


def _available():
    """{element name: rank} for the elements a shared chain can use that this container has. The rank
    is the registry's, so it carries GST_PLUGIN_FEATURE_RANK."""
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    names = ("nvvidconv", "nvv4l2h264enc", "nvv4l2h265enc", "x264enc", "h264parse", "h265parse", "capssetter")
    found = {n: Gst.ElementFactory.find(n) for n in names}
    return {n: int(f.get_rank()) for n, f in found.items() if f is not None}


def main():
    env = os.environ.get
    codec = codec_for(env("VIDEO_CAPS"))
    if codec is None:
        print(f"shared encoder: VIDEO_CAPS={env('VIDEO_CAPS')!r} is not H.264 or H.265", file=sys.stderr)
        return 3
    have = _available()
    encoder = select_encoder(codec, have)
    missing = [n for n in ("capssetter", "h264parse" if codec == "h264" else "h265parse") if n not in have]
    if encoder is None or missing:
        print(f"shared encoder: no {codec} encoder to share in this container"
              + (f" (missing {', '.join(missing)})" if missing else
                 " (H.265 needs the Jetson hardware encoder)" if codec == "h265" else ""), file=sys.stderr)
        return 3
    hw = "nvv4l2h264enc" if codec == "h264" else "nvv4l2h265enc"
    if encoder != hw and hw in have:
        print(f"shared encoder: {encoder} (rank {have[encoder]}) instead of {hw} (rank {have[hw]}): "
              "GST_PLUGIN_FEATURE_RANK decides, as it does for per-viewer encoding", file=sys.stderr)
    print(chain(codec, encoder, parse_bitrate(env("CAM_WEBRTC_BITRATE"), env("CAM_WEBRTC_START_BITRATE")),
                env("CAM_WEBRTC_X264_PRESET")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
