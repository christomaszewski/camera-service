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
from scale_plan import fit_within


def debayer_enabled(value):
    """CAM_WEBRTC_DEBAYER: auto/unset/anything-else -> debayer a Bayer stream to color;
    an explicit false form (any case -- YAML `false` arrives Python-cased as 'False') -> keep
    the raw mosaic."""
    return str(value if value is not None else "auto").strip().lower() \
        not in ("0", "false", "no", "off")


BAYER_PATTERNS = ("rggb", "bggr", "grbg", "gbrg")
_NEAR_HALF = 0.9


def debayer_full(value):
    """CAM_WEBRTC_DEBAYER=full: always the full-resolution debayer, never bayerbin."""
    return str(value if value is not None else "").strip().lower() == "full"


def bin_plan(width, height, max_size, pattern):
    """The size a Bayer stream is streamed at when it should be debayered by BINNING -- one RGB pixel
    per 2x2 cell, the bridge's `bayerbin` element -- instead of by bayer2rgb; else None.

    `max_size` is the CAM_WEBRTC_MAX_SIZE box (None = no bound). A full debayer of a frame that is then
    reduced to half size or less interpolates four pixels to keep one: 21 ms of a 5 MP frame on an
    Orin, most of what a viewer of such a camera costs. Binning is used when the scaled size is at or
    below half size -- or within 10% above it, where the bound is honoured by streaming the half-size
    frame as it is (2448x2048 asked to fit 1280 streams 1224x1024, not 1280x1070). A bound well above
    half size keeps the full debayer: binning would give away resolution that was asked for. So does a
    half-size frame that would be streamed as it is with an odd edge (4:2:0 and the hardware encoders
    want even sizes; the scaler rounds, binning cannot)."""
    if max_size is None or str(pattern).lower() not in BAYER_PATTERNS or width < 4 or height < 4:
        return None
    target = fit_within(width, height, *max_size)
    if target is None:
        return None
    half_w, half_h = width // 2, height // 2
    if half_w < _NEAR_HALF * target[0] or half_h < _NEAR_HALF * target[1]:
        return None
    scaled = fit_within(half_w, half_h, *max_size)
    if scaled is not None:
        return scaled
    return None if half_w % 2 or half_h % 2 else (half_w, half_h)


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
