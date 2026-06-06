"""Unit tests for the PTP-first timestamp fallback ladder.

These run WITHOUT PyGObject/Aravis (timestamps.py is importable standalone), so
they verify exactly the chunk-PTP logic that the Aravis *fake* camera CANNOT
exercise (the fake camera has no chunk/PTP support and only drives the fallback).

Run:  python3 core-driver/tests/test_timestamps.py      # or: pytest
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.timestamps import TimestampExtractor, TimestampSource  # noqa: E402


class FakeBuffer:
    """Stand-in for an Aravis buffer."""

    def __init__(self, system_ns, camera_ns, frame_id, chunks=None):
        self._system_ns = system_ns
        self._camera_ns = camera_ns
        self._frame_id = frame_id
        self._chunks = chunks  # e.g. {"ChunkTimestamp": .., "ChunkFrameID": ..} or None

    def get_system_timestamp(self):
        return self._system_ns

    def get_timestamp(self):
        return self._camera_ns

    def get_frame_id(self):
        return self._frame_id

    def has_chunks(self):
        return self._chunks is not None


class FakeParser:
    """Stand-in for an ArvChunkParser; reads from the buffer's chunk dict."""

    def get_integer_value(self, buf, name):
        return buf._chunks[name]


def test_ptp_chunk_is_primary():
    ex = TimestampExtractor(chunk_parser=FakeParser())
    assert ex.resolve_active_source(ptp_locked=True, chunks_enabled=True) is TimestampSource.PTP_CHUNK
    buf = FakeBuffer(system_ns=5000, camera_ns=4000, frame_id=7,
                     chunks={"ChunkTimestamp": 123456789, "ChunkFrameID": 42})
    s = ex.extract(buf)
    assert s.source is TimestampSource.PTP_CHUNK
    assert s.timestamp_ns == 123456789
    assert s.frame_id == 42          # chunk frame id wins over the GVSP block id (7)
    assert s.system_ns == 5000
    assert s.chunk_ns == 123456789   # all three are captured every frame for the experiment
    assert s.camera_ns == 4000


def test_resolve_falls_back_to_camera_without_parser():
    ex = TimestampExtractor(chunk_parser=None)
    assert ex.resolve_active_source(ptp_locked=False, chunks_enabled=False) is TimestampSource.CAMERA
    s = ex.extract(FakeBuffer(system_ns=9000, camera_ns=8000, frame_id=3))
    assert s.source is TimestampSource.CAMERA
    assert s.timestamp_ns == 8000
    assert s.frame_id == 3


def test_per_frame_degrade_when_chunk_missing():
    # Active source is PTP_CHUNK, but this particular frame carries no chunk data.
    ex = TimestampExtractor(chunk_parser=FakeParser())
    ex.resolve_active_source(ptp_locked=True, chunks_enabled=True)
    s = ex.extract(FakeBuffer(system_ns=5000, camera_ns=4000, frame_id=11, chunks=None))
    assert s.source is TimestampSource.CAMERA      # degraded for this frame
    assert s.timestamp_ns == 4000
    assert s.frame_id == 11


def test_camera_zero_falls_through_to_system():
    ex = TimestampExtractor(chunk_parser=None)
    ex.resolve_active_source(ptp_locked=False, chunks_enabled=False)  # -> CAMERA
    s = ex.extract(FakeBuffer(system_ns=7777, camera_ns=0, frame_id=1))
    assert s.source is TimestampSource.SYSTEM      # camera ts invalid (0) -> host arrival
    assert s.timestamp_ns == 7777


def test_system_preference_ignores_chunks():
    ex = TimestampExtractor(chunk_parser=FakeParser(), prefer=TimestampSource.SYSTEM)
    assert ex.resolve_active_source(ptp_locked=True, chunks_enabled=True) is TimestampSource.SYSTEM
    buf = FakeBuffer(system_ns=4242, camera_ns=8000, frame_id=2,
                     chunks={"ChunkTimestamp": 1, "ChunkFrameID": 2})
    s = ex.extract(buf)
    assert s.source is TimestampSource.SYSTEM
    assert s.timestamp_ns == 4242


def test_chunk_tick_conversion_non_ptp():
    # Non-PTP camera: chunk is raw ticks at, e.g., 125 MHz -> ns = raw * 1e9 / 125e6 = raw * 8.
    ex = TimestampExtractor(chunk_parser=FakeParser(), tick_frequency_hz=125_000_000)
    ex.resolve_active_source(ptp_locked=True, chunks_enabled=True)
    s = ex.extract(FakeBuffer(system_ns=0, camera_ns=0, frame_id=1,
                              chunks={"ChunkTimestamp": 1000, "ChunkFrameID": 5}))
    assert s.chunk_ns == 8000          # 1000 ticks * (1e9 / 125e6)
    assert s.timestamp_ns == 8000
    assert s.source is TimestampSource.PTP_CHUNK
    assert s.frame_id == 5


def test_chunk_used_without_ptp_lock():
    # chunks available but PTP not locked -> still use the chunk timestamp; record provenance
    ex = TimestampExtractor(chunk_parser=FakeParser())
    assert ex.resolve_active_source(ptp_locked=False, chunks_enabled=True) is TimestampSource.PTP_CHUNK
    assert ex.ptp_locked is False
    s = ex.extract(FakeBuffer(system_ns=1, camera_ns=2, frame_id=9,
                              chunks={"ChunkTimestamp": 42, "ChunkFrameID": 9}))
    assert s.source is TimestampSource.PTP_CHUNK and s.timestamp_ns == 42


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
