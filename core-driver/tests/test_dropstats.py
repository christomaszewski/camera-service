"""Tests for frame-drop accounting (pure logic; no GStreamer).

Run: python3 core-driver/tests/test_dropstats.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.dropstats import DropStats  # noqa: E402


def test_contiguous_no_gaps():
    d = DropStats()
    for fid in range(10):
        assert d.observe_frame(fid) == 0
    s = d.summary()
    assert (s["frames"], s["source_gaps"], s["frames_missing"]) == (10, 0, 0)


def test_single_gap_reports_size():
    d = DropStats()
    d.observe_frame(1)
    assert d.observe_frame(4) == 2          # 2 and 3 missing
    s = d.summary()
    assert (s["frames"], s["source_gaps"], s["frames_missing"]) == (2, 1, 2)


def test_multiple_gaps_accumulate():
    d = DropStats()
    for fid in (1, 2, 5, 6, 10):            # gaps: {3,4}=2 and {7,8,9}=3
        d.observe_frame(fid)
    s = d.summary()
    assert (s["source_gaps"], s["frames_missing"]) == (2, 5)


def test_reset_or_wrap_is_not_a_gap():
    d = DropStats()
    d.observe_frame(65534)
    d.observe_frame(65535)
    assert d.observe_frame(1) == 0          # wrap / reconnect reset, not a 64k "gap"
    assert d.summary()["source_gaps"] == 0


def test_duplicate_or_reorder_is_not_a_gap():
    d = DropStats()
    d.observe_frame(5)
    assert d.observe_frame(5) == 0          # duplicate
    assert d.observe_frame(4) == 0          # backward / reorder
    assert d.summary()["source_gaps"] == 0


def test_enqueue_failures_counted():
    d = DropStats()
    d.observe_frame(1)
    d.note_enqueue_failure()
    d.note_enqueue_failure()
    assert d.summary()["enqueue_failures"] == 2


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
