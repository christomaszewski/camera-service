"""16-bit samples <-> an 8-bit frame twice as wide, so a 16-bit stream can ride the 8-bit lossless path.

The Orin's hardware lossless encoder takes 8-bit 4:4:4 and nothing deeper, so a 16-bit camera (a
thermal core's Y16, Mono16, 10/12-bit Bayer in a 16-bit container) has recorded FFV1 on the CPU:
47 ms a frame for 640x512, three cores at 60 fps. Splitting each sample into its two bytes and laying
the halves side by side makes one GRAY8 frame of 2*width x height that the unchanged 8-bit recorder
takes -- measured bit-exact on the hardware encoder at 5.5-7.4 ms a frame
(docs/encoding-optimization/RESULTS.md). The recorder feeds the split frame; everything else still sees
the 16-bit one. The sidecar's `gray16_split_mode` says a recording is stored this way, and replay and
export join it again.

    | high bytes | low bytes |          one row of the stored frame
    0          width       2*width

Mode (recording.gray16_split):
  off   - record the 16-bit frame as it is (FFV1).
  fold  - the split, with the low byte mirrored (255 - low) wherever the high byte is odd.

Why fold: a smooth 16-bit ramp is a sawtooth in its low byte, and every carry is a 255 -> 0 cliff the
codec's predictor pays for. Mirroring the low byte on alternate high values turns the sawtooth into a
triangle wave: 254, 255 | 255, 254 instead of 254, 255 | 0, 1. It is exactly reversible because the
high byte, which decides the mirroring, is stored untouched. Measured on noisy frames the folded
split is 56% of raw where the plain one is 63%; plain is not offered.

NEVER put a lossy codec behind this: one code of error in the high half is 256 counts in the sample.
The recorder only splits in front of a lossless 8-bit encoder, and only uses the hardware one behind a
feed it has proven bit-exact on the host (recorder.probe_nv12_feed).

split/join are exact inverses for every 16-bit value and both byte orders. They need numpy, imported
when first used so that naming a mode does not.
"""
from __future__ import annotations

MODES = ("off", "fold")


def normalize_mode(value) -> str:
    """Coerce the CONFIG value to a mode in MODES. `true` means the one split there is; anything
    unrecognized means off -- the knob asks for an optimization, never for a different picture."""
    if value is True:
        return "fold"
    if value in (False, None):
        return "off"
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return "fold"
    return s if s in MODES else "off"


def stored_mode(value) -> str:
    """The mode a SIDECAR declares. Unlike the config knob this must not guess: a recording stored in a
    layout this build does not know cannot be read back as anything meaningful."""
    if value in (None, False, "", "off"):
        return "off"
    if value in MODES:
        return value
    raise ValueError(f"unsupported gray16_split_mode {value!r} (this build reads: {', '.join(MODES)})")


def geometry_ok(width: int, height: int) -> bool:
    """Both the split frame (2*width GRAY8) and the 16-bit frame need no row padding at an even width,
    so what the recorder is handed and what a decoder hands back are the tight layouts used here; the
    4:2:0 feed in front of the encoder wants an even height."""
    return width > 0 and height > 0 and width % 2 == 0 and height % 2 == 0


def _planes(big_endian: bool):
    return (0, 1) if big_endian else (1, 0)      # (index of the high byte, index of the low byte)


def _fold(np, hi, low, out) -> None:
    """out = low mirrored where hi is odd, written in place (this runs on the capture thread: no
    temporaries). 0 - (hi & 1) is 0x00 or 0xFF in uint8, and low ^ 0xFF == 255 - low, so the mirror is
    one XOR and is its own inverse."""
    np.bitwise_and(hi, 1, out=out)
    np.negative(out, out=out)
    np.bitwise_xor(out, low, out=out)


def split(frame, width: int, height: int, big_endian: bool = False) -> bytes:
    """A width x height 16-bit frame -> the 2*width x height GRAY8 frame that stores it (mode fold)."""
    import numpy as np
    if len(frame) != width * height * 2:
        raise ValueError(f"gray16 split: {len(frame)} bytes is not a {width}x{height} 16-bit frame")
    hi_i, lo_i = _planes(big_endian)
    a = np.frombuffer(frame, np.uint8).reshape(height, width, 2)
    out = np.empty((height, 2 * width), np.uint8)
    hi, lo = out[:, :width], out[:, width:]
    hi[:] = a[..., hi_i]
    _fold(np, hi, a[..., lo_i], lo)
    return out.tobytes()


def join(stored, width: int, height: int, big_endian: bool = False) -> bytes:
    """The exact inverse of split: the 2*width x height GRAY8 frame -> the 16-bit frame."""
    import numpy as np
    if len(stored) != width * height * 2:
        raise ValueError(f"gray16 join: {len(stored)} bytes is not a {2 * width}x{height} 8-bit frame")
    hi_i, lo_i = _planes(big_endian)
    a = np.frombuffer(stored, np.uint8).reshape(height, 2 * width)
    hi = a[:, :width]
    out = np.empty((height, width, 2), np.uint8)
    out[..., hi_i] = hi
    _fold(np, hi, a[:, width:], out[..., lo_i])
    return out.tobytes()
