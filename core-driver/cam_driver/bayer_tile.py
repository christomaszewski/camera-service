"""CFA mosaic <-> 4-quadrant sub-plane tiling (+ optional reversible colour transform), 8-bit lossless.

A Bayer mosaic interleaves four colour phases on a 2x2 grid, so neighbouring pixels are *different*
colours -- a high-frequency checkerboard that defeats a lossless codec's spatial predictor and (worse,
for video) its motion compensation. Deinterleaving into the four sub-planes and packing them as the four
quadrants of one same-size frame makes each quadrant a smooth same-colour image. The recorder feeds the
*tiled* frame to the (unchanged) lossless encoder; everything else still sees the mosaic.

Modes (recording.bayer_tile):
  off        - no tiling (record the mosaic).
  plain/true - quadrant tiling only. Pattern-agnostic (groups by parity); the BIG win (kills the CFA).
  green_diff - plain tiling + replace the 2nd green quadrant with (G2 - G1) re-centred at 128. The two
               greens are the most-correlated pair, so this residual is tiny and almost always helps --
               even on a predictive codec (HEVC). Small, safe, near-free.
  rct        - plain tiling + a reversible colour transform: keep one green G1 as the luma/reference
               quadrant and store R, B, G2 as (x - G1 + 128). Captures the R/B<->G luminance correlation
               the codec can't (the quadrants sit far apart). Higher ceiling, but the 8-bit modular wrap
               taxes saturated colours -- and a *predictive* codec feels that more than an entropy coder,
               so on HW HEVC it's a measure-it, not a default.

Why +128 (not +0): we're locked to 8-bit (the Jetson HW-lossless / NV24-Y path), so residuals must wrap
mod 256. Centred at 128 a small +/- diff stays near smooth mid-grey; centred at 0 a small negative would
become ~255 next to 0s -- recreating exactly the high-frequency jumps tiling removed (poison for HEVC
intra prediction). Centring is mandatory for the predictive HW codec, not just a nicety.

green_diff/rct are pattern-aware (need to know which quadrants are R/G/B); the Bayer pattern is in the
sidecar. tile/untile are exact inverses, so mosaic -> tile -> lossless-encode -> decode -> untile is
bit-for-bit. 8-bit only.
"""
from __future__ import annotations

import numpy as np

MODES = ("off", "plain", "green_diff", "rct")

# pattern (top-left 2x2 in reading order) -> phase coords (row%2, col%2) of each colour. G1 = reference
# green (first in reading order), G2 = the other green.
_PHASES = {
    "rggb": {"R": (0, 0), "G1": (0, 1), "G2": (1, 0), "B": (1, 1)},
    "grbg": {"R": (0, 1), "G1": (0, 0), "G2": (1, 1), "B": (1, 0)},
    "gbrg": {"R": (1, 0), "G1": (0, 0), "G2": (1, 1), "B": (0, 1)},
    "bggr": {"R": (1, 1), "G1": (0, 1), "G2": (1, 0), "B": (0, 0)},
}


def normalize_mode(value) -> str:
    """Coerce the config value (bool or string) to a mode in MODES; unknown -> 'off'."""
    if value is True:
        return "plain"
    if value in (False, None):
        return "off"
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes"):
        return "plain"
    if s in ("", "0", "false", "off", "no", "none"):
        return "off"
    return s if s in MODES else "off"


def _phases(pattern):
    return _PHASES.get((pattern or "rggb").lower(), _PHASES["rggb"])


def _quad_slice(phase, h2, w2):
    r, c = phase
    return (slice(r * h2, (r + 1) * h2), slice(c * w2, (c + 1) * w2))


def tile_cfa(mosaic: bytes, w: int, h: int, mode: str = "plain", pattern: str = "rggb") -> bytes:
    """Deinterleave a w*h 8-bit CFA mosaic into 4 quadrants (+ optional colour transform). Same w*h."""
    h2, w2 = h // 2, w // 2
    a = np.frombuffer(mosaic, np.uint8).reshape(h2, 2, w2, 2)         # [i, row-phase, k, col-phase]
    t = np.array(a.transpose(1, 0, 3, 2)).reshape(h, w)              # writable plain-tiled copy
    if mode in ("green_diff", "rct"):
        ph = _phases(pattern)
        g1 = t[_quad_slice(ph["G1"], h2, w2)].astype(np.int16)      # reference green (kept raw)
        targets = [ph["G2"]] + ([ph["R"], ph["B"]] if mode == "rct" else [])
        for phase in targets:
            sl = _quad_slice(phase, h2, w2)
            t[sl] = ((t[sl].astype(np.int16) - g1 + 128) & 0xFF).astype(np.uint8)
    return t.tobytes()


def untile_cfa(tiled: bytes, w: int, h: int, mode: str = "plain", pattern: str = "rggb") -> bytes:
    """Inverse of tile_cfa: undo the colour transform (if any), then reassemble the mosaic."""
    h2, w2 = h // 2, w // 2
    t = np.frombuffer(tiled, np.uint8).reshape(h, w).copy()
    if mode in ("green_diff", "rct"):
        ph = _phases(pattern)
        g1 = t[_quad_slice(ph["G1"], h2, w2)].astype(np.int16)     # untouched -> valid reference
        targets = [ph["G2"]] + ([ph["R"], ph["B"]] if mode == "rct" else [])
        for phase in targets:
            sl = _quad_slice(phase, h2, w2)
            t[sl] = ((t[sl].astype(np.int16) - 128 + g1) & 0xFF).astype(np.uint8)
    a = t.reshape(2, h2, 2, w2)
    return np.ascontiguousarray(a.transpose(1, 0, 3, 2)).tobytes()


def _selftest() -> None:
    rng = np.random.default_rng(0)
    # 1) round-trip is exact for every mode x pattern on random data.
    for w, h in ((8, 6), (512, 512), (1936, 1216)):
        m = rng.integers(0, 256, size=h * w, dtype=np.uint8).tobytes()
        for pat in _PHASES:
            for mode in MODES:
                t = tile_cfa(m, w, h, mode, pat)
                assert len(t) == len(m), f"{mode}/{pat}: size changed"
                assert untile_cfa(t, w, h, mode, pat) == m, f"{mode}/{pat}: not reversible"
    # 2) plain tiling really groups by phase (TL quadrant == the (0::2,0::2) sub-plane).
    W, H = 64, 48
    m = rng.integers(0, 256, size=H * W, dtype=np.uint8).reshape(H, W)
    t = np.frombuffer(tile_cfa(m.tobytes(), W, H, "plain"), np.uint8).reshape(H, W)
    assert np.array_equal(t[: H // 2, : W // 2], m[0::2, 0::2]), "TL quadrant != phase (0,0)"
    # 3) on CORRELATED colour (a smooth luma ramp -> R~=G~=B), the residual quadrants shrink toward 128.
    yy, xx = np.mgrid[0:H, 0:W]
    ramp = ((xx + yy) % 200 + 20).astype(np.uint8)            # smooth, all channels equal -> max correlation
    raw_spread = float(np.abs(ramp[0::2, 0::2].astype(int) - 128).mean())   # R quadrant energy about 128
    tr = np.frombuffer(tile_cfa(ramp.tobytes(), W, H, "rct", "rggb"), np.uint8).reshape(H, W)
    r_resid = float(np.abs(tr[: H // 2, : W // 2].astype(int) - 128).mean())  # R-G residual energy about 128
    assert r_resid < raw_spread, f"rct residual ({r_resid:.1f}) should be < raw ({raw_spread:.1f})"
    print(f"bayer_tile self-test PASS (round-trip all modes/patterns; rct residual {r_resid:.1f} "
          f"vs raw {raw_spread:.1f} on correlated colour)")


if __name__ == "__main__":
    _selftest()
