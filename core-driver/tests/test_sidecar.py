"""Tests for the sidecar writer's FAILURE path -- the one that used to hang a vehicle.

The happy path is covered end-to-end by tools/dev_test.sh (it counts CSV rows against the .mkv).
What was untested, and what broke, is what happens when the writer thread dies: the bounded queue
fills, and stop()'s untimed put() then blocked the MAIN thread forever against a consumer that no
longer existed. With `recording: enabled: false` there is no GStreamer bus ERROR to fail the run
fast instead, so the container just hung until SIGKILL.

No GStreamer here -- sidecar.py is pure stdlib, so this runs anywhere.

Run: python3 core-driver/tests/test_sidecar.py
"""
import json
import os
import queue
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.sidecar import SidecarHeader, SidecarWriter


def _header():
    return SidecarHeader(
        created_unix_s=1.0, base_timestamp_ns=5, timestamp_source="system", ptp_synced=False,
        pixel_format="Mono8", bayer_pattern=None, bits_per_pixel=8, width=4, height=4,
        tick_frequency_hz=0)


def _writer_that_cannot_open(tmp):
    """A SidecarWriter whose CSV path is a DIRECTORY, so the writer thread's open() raises
    IsADirectoryError -- a real OSError on the real code path, no monkeypatching."""
    base = os.path.join(tmp, "cam")
    os.mkdir(base + ".csv")
    w = SidecarWriter(base)
    w.start()
    w._thread.join(timeout=5)          # let it hit the failure
    assert not w._thread.is_alive(), "writer thread should have exited on the open() error"
    return w


def test_writer_failure_is_flagged_not_raised():
    with tempfile.TemporaryDirectory() as tmp:
        w = _writer_that_cannot_open(tmp)
        assert w._failed, "an I/O failure in the writer must be recorded, not swallowed"


def test_add_is_a_noop_once_the_writer_has_failed():
    # Otherwise every subsequent frame enqueues into a queue nothing drains, which is what filled it.
    with tempfile.TemporaryDirectory() as tmp:
        w = _writer_that_cannot_open(tmp)
        before = w._q.qsize()
        for _ in range(50):
            w.add(_FakeStamp(), 0)
        assert w._q.qsize() == before


def test_stop_does_not_block_on_a_full_queue_with_a_dead_writer():
    # THE regression: pre-fix this blocked forever on `self._q.put(_SENTINEL)`.
    with tempfile.TemporaryDirectory() as tmp:
        w = _writer_that_cannot_open(tmp)
        while True:                     # fill it the way a live capture would have
            try:
                w._q.put_nowait(("row",))
            except queue.Full:
                break
        t0 = time.monotonic()
        w.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0, f"stop() took {elapsed:.1f}s; it must not wait on a dead consumer"


def test_failed_writer_is_attested_in_the_json_summary():
    # A truncated CSV must be self-describing: a consumer must not read a missing frame_id as
    # "this frame was never captured".
    with tempfile.TemporaryDirectory() as tmp:
        w = _writer_that_cannot_open(tmp)
        w.write_header(_header())
        w.write_summary({"frames": 10})
        with open(os.path.join(tmp, "cam.json")) as f:
            data = json.load(f)
        assert data.get("sidecar_csv_failed") is True
        assert data["drops"]["frames"] == 10
        assert data["base_timestamp_ns"] == 5   # the header survives the summary merge


def test_healthy_writer_records_no_failure_flag():
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "cam")
        w = SidecarWriter(base)
        w.start()
        w.add(_FakeStamp(), 123)
        w.write_header(_header())
        w.stop()
        assert not w._failed
        with open(base + ".csv") as f:
            rows = f.read().strip().splitlines()
        assert len(rows) == 2                      # header + the one frame
        w2 = SidecarWriter(base)
        w2.write_summary({"frames": 1})
        with open(base + ".json") as f:
            assert "sidecar_csv_failed" not in json.load(f)


class _FakeStamp:
    """Minimal stand-in for FrameStamp (which lives behind the gi-importing timestamps module)."""
    frame_id = 1
    timestamp_ns = 2
    chunk_ns = None
    camera_ns = 3
    system_ns = 4

    class source:
        value = "system"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
