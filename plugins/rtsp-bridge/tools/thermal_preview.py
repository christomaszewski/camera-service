"""16-bit mono -> 8-bit operator-preview normalization (the WebRTC thermal stretch).

A radiometric camera (e.g. a Y16 thermal core) packs its counts LSB-aligned in a 16-bit
container, so the naive 16->8 conversion (videoconvert keeps the TOP byte) renders near-black
with the real detail discarded. This maps each frame's OCCUPIED range onto the full 8-bit range
instead: a percentile window (robust to hot/dead pixels) smoothed across frames with an EMA so
the preview doesn't flicker as the scene shifts the histogram.

PREVIEW-ONLY: the recording and the ROS topic carry the original 16-bit data untouched.
Pure numpy -- no GStreamer -- so it is unit-testable anywhere (see test_thermal_preview.py).
"""
import numpy as np

DEFAULT_WINDOW = (1.0, 99.0)


def parse_window(spec):
    """Parse a CAM_WEBRTC_NORMALIZE value into a (lo, hi) percentile window.

    'auto' (or any plain truthy form) -> the (1, 99) default; 'lo:hi' -> that window
    (e.g. '5:99.5'). Raises ValueError on junk or an out-of-order window -- the caller
    warns and falls back to the default."""
    s = (spec or "").strip().lower()
    if s in ("", "auto", "1", "true", "yes", "on"):
        return DEFAULT_WINDOW
    lo_s, sep, hi_s = s.partition(":")
    if not sep:
        raise ValueError(f"expected 'auto' or 'lo:hi', got {spec!r}")
    lo, hi = float(lo_s), float(hi_s)
    if not (0.0 <= lo < hi <= 100.0):
        raise ValueError(f"percentile window out of order/range: {spec!r}")
    return (lo, hi)


class PercentileStretch:
    """Callable HxW uint16 frame -> HxW uint8, windowed to EMA-smoothed percentiles.

    smooth is the EMA weight on the PREVIOUS window (0 = follow each frame exactly,
    0.9 = move ~10% of the way per frame -- damps flicker without lagging a real
    scene change by more than a few frames at preview rates)."""

    def __init__(self, lo_pct=DEFAULT_WINDOW[0], hi_pct=DEFAULT_WINDOW[1], smooth=0.9):
        if not (0.0 <= lo_pct < hi_pct <= 100.0):
            raise ValueError(f"bad percentile window [{lo_pct}, {hi_pct}]")
        if not (0.0 <= smooth < 1.0):
            raise ValueError(f"bad smoothing factor {smooth}")
        self.lo_pct, self.hi_pct = float(lo_pct), float(hi_pct)
        self.smooth = float(smooth)
        self._lo = self._hi = None          # EMA state

    def __call__(self, frame):
        lo, hi = np.percentile(frame, (self.lo_pct, self.hi_pct))
        if self._lo is None:
            self._lo, self._hi = float(lo), float(hi)
        else:
            a = self.smooth
            self._lo = a * self._lo + (1.0 - a) * float(lo)
            self._hi = a * self._hi + (1.0 - a) * float(hi)
        span = max(self._hi - self._lo, 1.0)   # flat frame: no divide-by-zero, renders black
        out = (frame.astype(np.float32) - self._lo) * (255.0 / span)
        return np.clip(out, 0.0, 255.0).astype(np.uint8)
