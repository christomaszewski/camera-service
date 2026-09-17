"""The preview RTP timeline must follow wall time, not the replay's historical clock.

Run in the GStreamer dev container: python3 core-driver/tests/test_preview_clock.py.
Capture/recording timestamps are tested separately in test_pipeline_pts.py.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cam_driver.pipeline import CapturePipeline

NS = 1_000_000_000


def test_gap_filled_by_held_frames_is_not_counted_twice():
    p = CapturePipeline(SimpleNamespace(), SimpleNamespace(finite=True))
    with patch("cam_driver.pipeline.time.monotonic", side_effect=[100, 101, 102, 102.01]):
        assert p._transport_pts(0, held=False) == 0
        assert p._transport_pts(0, held=True) == NS
        assert p._transport_pts(0, held=True) == 2 * NS
        # A new capture arrives after the two-second gap; old behavior added two seconds AGAIN.
        assert abs(p._transport_pts(2 * NS, held=False) - 2.01 * NS) < 2


def test_two_previews_with_different_capture_progress_use_the_same_elapsed_media_time():
    a = CapturePipeline(SimpleNamespace(), SimpleNamespace(finite=True))
    b = CapturePipeline(SimpleNamespace(), SimpleNamespace(finite=True))
    with patch("cam_driver.pipeline.time.monotonic", return_value=100):
        a._transport_pts(0, held=False)
        b._transport_pts(0, held=False)
    with patch("cam_driver.pipeline.time.monotonic", return_value=101):
        # Fast/slow playback changes CONTENT progression, not the RTP media clock's rate.
        assert a._transport_pts(4 * NS, held=False) == NS
        assert b._transport_pts(NS // 2, held=False) == NS
    with patch("cam_driver.pipeline.time.monotonic", return_value=102):
        assert a._transport_pts(4 * NS, held=True) == 2 * NS
        assert b._transport_pts(NS, held=False) == 2 * NS


def test_live_capture_preserves_its_original_transport_timing():
    p = CapturePipeline(SimpleNamespace(), SimpleNamespace(finite=False))
    with patch("cam_driver.pipeline.time.monotonic", side_effect=[100, 101]):
        assert p._transport_pts(0, held=False) == 0
        assert p._transport_pts(900_000_000, held=False) == 900_000_000


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
