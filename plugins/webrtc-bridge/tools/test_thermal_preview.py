"""Tests for the 16->8 preview stretch (pure numpy; no GStreamer needed).

Run: python3 plugins/webrtc-bridge/tools/test_thermal_preview.py   (or pytest)
"""
import importlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from thermal_preview import DEFAULT_WINDOW, PercentileStretch, parse_window  # noqa: E402


def test_lsb_aligned_data_uses_full_output_range():
    # the motivating case: 14-bit counts in a 16-bit container (top byte ~0 -> naive convert = black)
    frame = np.linspace(20000, 20300, 640 * 512, dtype=np.uint16).reshape(512, 640)
    out = PercentileStretch()(frame)
    assert out.dtype == np.uint8 and out.shape == frame.shape
    assert out.min() == 0 and out.max() == 255          # occupied range spread to full scale
    assert 100 < int(np.median(out)) < 155              # interior maps near mid-gray


def test_flat_frame_is_black_not_crash():
    out = PercentileStretch()(np.full((8, 8), 21000, dtype=np.uint16))
    assert out.max() == 0                                # span guard: zero spread, no div-by-zero


def test_hot_pixel_does_not_crush_the_scene():
    frame = np.full((100, 100), 1000, dtype=np.uint16)
    frame[:50] += np.arange(100, dtype=np.uint16)        # scene detail: +0..99 counts
    frame[0, 0] = 65535                                  # dead/hot pixel
    out = PercentileStretch()(frame)
    # a min-max stretch would map the scene to ~0; the percentile window ignores the outlier
    assert int(out[25].max()) > 200
    assert out[0, 0] == 255                              # outlier clips, doesn't dominate


def test_ema_smooths_a_histogram_jump():
    s = PercentileStretch(smooth=0.9)
    cold = np.random.default_rng(0).integers(1000, 1100, (64, 64)).astype(np.uint16)
    hot = cold + 5000
    s(cold)
    lo_after_first = s._lo
    s(hot)                                               # window moves only ~10% toward the jump
    assert s._lo - lo_after_first < 0.2 * 5000


def test_first_frame_initializes_window_exactly():
    s = PercentileStretch()
    frame = np.linspace(500, 800, 1000, dtype=np.uint16).reshape(20, 50)
    out = s(frame)
    assert out.min() == 0 and out.max() == 255           # no warm-up half-frame


def test_parse_window():
    assert parse_window("auto") == DEFAULT_WINDOW
    assert parse_window("") == DEFAULT_WINDOW
    assert parse_window("true") == DEFAULT_WINDOW
    assert parse_window("5:99.5") == (5.0, 99.5)
    for bad in ("garbage", "99:5", "0:101", "5"):
        try:
            parse_window(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_bridge_imports_the_shared_implementation():
    assert PercentileStretch.__module__ in ("cam_driver.thermal_preview", "_thermal_preview")
    implementation = importlib.import_module(PercentileStretch.__module__)
    assert PercentileStretch is implementation.PercentileStretch
    assert parse_window is implementation.parse_window
    assert DEFAULT_WINDOW == implementation.DEFAULT_WINDOW


def test_bundled_bridge_import_works_without_the_core_package():
    implementation = importlib.import_module(PercentileStretch.__module__)
    frame = np.arange(256, dtype=np.uint16).reshape(16, 16) + 20000
    expected = PercentileStretch()(frame).tobytes().hex()
    with tempfile.TemporaryDirectory() as directory:
        shutil.copyfile(Path(__file__).with_name("thermal_preview.py"),
                        Path(directory) / "thermal_preview.py")
        shutil.copyfile(implementation.__file__, Path(directory) / "_thermal_preview.py")
        result = subprocess.run([sys.executable, "-c", """
import numpy as np
from thermal_preview import PercentileStretch
assert PercentileStretch.__module__ == '_thermal_preview'
frame = np.arange(256, dtype=np.uint16).reshape(16, 16) + 20000
print(PercentileStretch()(frame).tobytes().hex())
"""], cwd=directory, check=True, capture_output=True, text=True)
        assert result.stdout.strip() == expected


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
