"""CFA mosaic <-> 4-quadrant sub-plane tiling (lossless, pattern-agnostic).

A Bayer mosaic interleaves four colour phases on a 2x2 grid, so neighbouring pixels are
*different* colours -- a high-frequency checkerboard that defeats a lossless codec's spatial
predictor and (worse, for video) its motion compensation: a 1-px shift changes the CFA phase,
so the previous frame is a poor predictor.

Deinterleaving the mosaic into its four sub-sampled planes and packing them as the four quadrants
of one same-size frame turns each quadrant into a smooth same-colour image, so prediction and
motion compensation stop fighting the CFA. The recorder feeds the *tiled* frame to the (unchanged)
lossless encoder; everything else still sees the mosaic.

Layout (W,H even): quadrant TL/TR/BL/BR = phase (row%2,col%2) = (0,0)/(0,1)/(1,0)/(1,1). It is
*pattern-agnostic* -- the grouping is by parity, not colour; the Bayer pattern (recorded in the
sidecar) is only needed to re-mosaic + demosaic on playback. `untile_cfa` is the exact inverse, so
mosaic -> tile -> lossless-encode -> decode -> untile reproduces the mosaic bit-for-bit.

8-bit only (the HW-lossless path is 8-bit; 16-bit Bayer isn't tiled).
"""
from __future__ import annotations

import numpy as np


def tile_cfa(mosaic: bytes, w: int, h: int) -> bytes:
    """Deinterleave a w*h 8-bit CFA mosaic into 4 quadrant sub-planes (same w*h bytes)."""
    a = np.frombuffer(mosaic, np.uint8).reshape(h // 2, 2, w // 2, 2)   # [i, row-phase, k, col-phase]
    return np.ascontiguousarray(a.transpose(1, 0, 3, 2)).tobytes()      # -> [row-phase, i, col-phase, k]


def untile_cfa(tiled: bytes, w: int, h: int) -> bytes:
    """Inverse of tile_cfa: reassemble the original mosaic from the 4 quadrants."""
    a = np.frombuffer(tiled, np.uint8).reshape(2, h // 2, 2, w // 2)
    return np.ascontiguousarray(a.transpose(1, 0, 3, 2)).tobytes()


def _selftest() -> None:
    rng = np.random.default_rng(0)
    for w, h in ((8, 6), (512, 512), (1936, 1216)):
        m = rng.integers(0, 256, size=h * w, dtype=np.uint8).tobytes()
        t = tile_cfa(m, w, h)
        assert len(t) == len(m), "tiling must preserve size"
        assert untile_cfa(t, w, h) == m, "untile(tile(x)) must be identity"
        # phase grouping: quadrant TL must equal the (0::2,0::2) sub-plane of the mosaic
        ma = np.frombuffer(m, np.uint8).reshape(h, w)
        ta = np.frombuffer(t, np.uint8).reshape(h, w)
        assert np.array_equal(ta[: h // 2, : w // 2], ma[0::2, 0::2]), "TL quadrant != phase (0,0)"
        assert np.array_equal(ta[h // 2 :, w // 2 :], ma[1::2, 1::2]), "BR quadrant != phase (1,1)"
    print("bayer_tile self-test PASS (round-trip + phase grouping)")


if __name__ == "__main__":
    _selftest()
