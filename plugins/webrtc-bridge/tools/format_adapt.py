"""Input format adaptation for the self-describing (unixfd) transport (pure logic; no
GStreamer -- unit-testable anywhere, see test_format_adapt.py).

The problem this solves: on unixfd the STREAM decides its own caps (the core publishes what the
live device actually produces -- video/x-bayer for an 8-bit CFA camera, video/x-raw GRAY8/16 for
mono), while the bridge's env (CAM_BAYER, derived from the config pixel_format) is only a
PREDICTION. A front-end hardwired from env dies not-negotiated -- in a container restart loop --
whenever the two disagree: config vs device pixel format drift, a camera that rejected the
configured format, CAM_BAYER unset, a 16-bit Bayer sensor riding as GRAY16, or
CAM_WEBRTC_DEBAYER=false against a Bayer stream (videoconvert takes no video/x-bayer). So
run.sh plants an inert `identity name=fmt_tap` seam and bridge_stream.py resolves it from the
FIRST caps the stream actually carries, using the decision below.
"""


def debayer_enabled(value):
    """CAM_WEBRTC_DEBAYER: auto/unset/anything-else -> debayer a Bayer stream to color;
    an explicit false form (any case -- YAML `false` arrives Python-cased as 'False') -> keep
    the raw mosaic."""
    return str(value if value is not None else "auto").strip().lower() \
        not in ("0", "false", "no", "off")


def adapt_for_input(media_type, width, height, framerate, debayer):
    """Decide the element to splice in at the fmt_tap seam for the ACTUAL input caps.

    Returns (element_factory, caps_property_string_or_None), or None for straight passthrough:
      video/x-bayer + debayer     -> ("bayer2rgb", None): color for the viewer (the element
                                     reads the CFA pattern off the caps)
      video/x-bayer + no debayer  -> ("capssetter", full GRAY8 caps): the 8-bit mosaic is
                                     byte-identical to a GRAY8 plane, so previewing it raw is a
                                     caps-only rewrite -- no data copy
      anything else               -> None (GRAY8/GRAY16 mono, color formats: downstream
                                     videoconvert already handles them)
    """
    if media_type != "video/x-bayer":
        return None
    if debayer:
        return ("bayer2rgb", None)
    caps = "video/x-raw,format=GRAY8"
    if width and height:
        caps += ",width={},height={}".format(width, height)
    if framerate:
        caps += ",framerate={}".format(framerate)
    return ("capssetter", caps)
