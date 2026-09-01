"""Encode-side downscale plan for the webrtc preview (pure logic; no GStreamer -- unit-testable
anywhere, see test_scale_plan.py).

CAM_WEBRTC_MAX_SIZE bounds the geometry fed to webrtcsink's encoder: run.sh inserts
`videoscale name=fmt_scale ! capsfilter name=scale_caps` right after the format seam, and
bridge_stream.py pins the capsfilter from the caps the stream ACTUALLY carries (a pad probe on the
scaler's sink pad -- the same "the stream decides, env only bounds" contract as the fmt_tap seam),
using the decision below. What this removes is everything downstream of the scaler at full
resolution: videoconvert, the (software) encoder, and the overlay / normalize passes. bayer2rgb,
when present, sits upstream and still runs at sensor resolution.
"""

_OFF = ("", "0", "off", "false", "no", "none")


def parse_max_size(spec):
    """CAM_WEBRTC_MAX_SIZE -> (max_w, max_h), or None for off.

    Forms: 'WxH' = a bounding box (e.g. 1280x720); 'N' = the same bound on both axes (e.g. 1280 --
    a longest-edge cap that works for either orientation). Case/whitespace-insensitive; the off
    forms (unset/empty/0/off/false/no/none) return None. Raises ValueError on anything else so the
    caller can warn and fall back to passthrough."""
    s = "".join(str(spec if spec is not None else "").split()).lower()
    if s in _OFF:
        return None
    parts = s.split("x")
    if len(parts) == 1:
        parts = [s, s]
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("expected WxH or N, got {!r}".format(spec))
    w, h = int(parts[0]), int(parts[1])
    if w < 2 or h < 2:
        raise ValueError("bound must be >= 2 pixels per axis, got {!r}".format(spec))
    return (w, h)


def fit_within(width, height, max_w, max_h):
    """The (w, h) to scale a width x height frame to so it fits inside max_w x max_h, or None when
    it already fits -- NEVER upscale: a frame smaller than the box streams as-is (and the scaler
    stays in passthrough, zero cost).

    Aspect is preserved: the binding axis lands on its bound, the other floats below it. Both
    outputs are rounded DOWN to even -- odd sizes upset 4:2:0 chroma subsampling and some HW
    encoder paths (nvvidconv/NVENC), and the sub-pixel aspect error is invisible. Integer math and
    floor (not round), so the result can never exceed the box by a rounding hair."""
    if width <= 0 or height <= 0:
        raise ValueError("bad input geometry {}x{}".format(width, height))
    if width <= max_w and height <= max_h:
        return None
    if max_w * height <= max_h * width:          # width is the binding axis
        ow, oh = max_w, height * max_w // width
    else:                                        # height binds
        ow, oh = width * max_h // height, max_h
    return (max(2, ow // 2 * 2), max(2, oh // 2 * 2))


def scale_caps(out_w, out_h):
    """The capsfilter string pinning the scaler output. pixel-aspect-ratio is pinned square on
    purpose: with only width/height fixed, videoscale would 'preserve' the display aspect by
    emitting a near-1 PAR (e.g. 1023/1024) that lands in the SDP/VUI and makes browsers stretch the
    picture by a hair. run.sh sets add-borders=false on the scaler, so a square PAR never
    letterboxes either -- the rounding is absorbed as an invisible sub-pixel stretch."""
    return "video/x-raw,width={},height={},pixel-aspect-ratio=1/1".format(out_w, out_h)
