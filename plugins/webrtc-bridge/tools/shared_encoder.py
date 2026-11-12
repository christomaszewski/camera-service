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

BITRATE_DEFAULT = 4_000_000
PROFILES = ("constrained-baseline", "baseline", "main", "high")
_CODEC_OF = {"video/x-h264": "h264", "video/x-h265": "h265"}
# Fields h26Xparse adds to its caps only after parsing the stream headers. Declared up front (the
# pipelines here are 8-bit 4:2:0 progressive by construction: I420 or NVMM NV12 into the encoder).
_PARSED_FIELDS = ("coded-picture-structure=(string)frame,chroma-format=(string)4:2:0,"
                  "bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8")
QUEUE = "queue name=shared_enc_q leaky=downstream max-size-buffers=2"


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
    constrained-baseline, which every browser decodes."""
    p = str(value or "").strip().lower()
    return p if p in PROFILES else "constrained-baseline"


def select_encoder(codec, available):
    """The encoder element for `codec` among the element names in `available`: the Jetson hardware
    encoder when its pieces are there, else software for H.264, else None."""
    hw = "nvv4l2h264enc" if codec == "h264" else "nvv4l2h265enc"
    if hw in available and "nvvidconv" in available:
        return hw
    if codec == "h264" and "x264enc" in available:
        return "x264enc"
    return None


def pin_caps(encoder, codec, profile=None, level=None):
    """Caps for the capsfilter after the encoder, so the parser's FIRST announcement is its last.
    nvv4l2h264enc leaves profile and level out of its caps and takes both from downstream: pin both
    (the level must be the one the geometry needs -- h264_level). x264enc announces its own level and
    takes only the profile. H.265 encoders announce everything themselves."""
    base = f"video/x-{codec},stream-format=byte-stream,alignment=au"
    if codec != "h264":
        return base
    caps = base + f",profile={parse_profile(profile)}"
    if encoder == "nvv4l2h264enc" and level:
        caps += f",level=(string){level}"
    return caps


def chain(codec, encoder, bitrate, x264_preset="ultrafast"):
    """The launch fragment from raw I420 to the encoded stream webrtcsink takes: a queue, the encoder
    (named shared_enc), the capsfilter bridge_stream completes at run time (shared_enc_caps), the
    parser and the capssetter that declares what the parser would otherwise add later.

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
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    names = ("nvvidconv", "nvv4l2h264enc", "nvv4l2h265enc", "x264enc", "h264parse", "h265parse", "capssetter")
    return {n for n in names if Gst.ElementFactory.find(n) is not None}


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
    print(chain(codec, encoder, parse_bitrate(env("CAM_WEBRTC_BITRATE"), env("CAM_WEBRTC_START_BITRATE")),
                env("CAM_WEBRTC_X264_PRESET")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
