"""Tests for the playback helpers (run discovery, sidecar re-stamping, pacing math)
-- the pure parts of the replay source; no GStreamer needed.

Run: python3 core-driver/tests/test_replay.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.playback import (  # noqa: E402
    Pacer, discover_run, load_stamps, median_interval_ns, shift_stamp,
)
from cam_driver.timestamps import FrameStamp, TimestampSource  # noqa: E402

_HEADER = ('{"base_timestamp_ns": 100, "timestamp_source": "camera", "ptp_synced": false,'
           ' "pixel_format": "Mono8", "bayer_pattern": null, "bits_per_pixel": 8,'
           ' "width": 64, "height": 32, "tick_frequency_hz": 0, "cfa_tile_mode": "off"}')
_CSV = ("frame_id,pts_ns,timestamp_ns,source,chunk_ns,camera_ns,system_ns\n"
        "10,0,1000,camera,,1000,1005\n"
        "11,40,1040,ptp_chunk,1040,1041,1045\n"
        "13,120,1120,weird-source,,1120,1125\n")


def _make_run(d, prefix, parts=2, header=_HEADER, csv=_CSV, mtime=None):
    base = os.path.join(d, prefix)
    with open(base + ".json", "w") as f:
        f.write(header)
    with open(base + ".csv", "w") as f:
        f.write(csv)
    for i in range(parts):
        open(f"{base}-{i:05d}.mkv", "wb").close()
    if mtime is not None:
        os.utime(base + ".json", (mtime, mtime))
    return base


def test_discover_run_prefix_and_dir():
    d = tempfile.mkdtemp()
    base = _make_run(d, "cam-20260827-041627")
    for path in (base, base + ".json", d):    # prefix, its json, or the directory
        run = discover_run(path)
        assert run.base == base
        assert len(run.mkv_paths) == 2 and run.mkv_paths == sorted(run.mkv_paths)
        assert run.header["pixel_format"] == "Mono8"
        assert run.mkv_glob == base + "-*.mkv"


def test_discover_run_multiple_picks_newest_run_pins():
    d = tempfile.mkdtemp()
    old = _make_run(d, "cam-20260827-041627", mtime=time.time() - 100)
    new = _make_run(d, "cam-20260827-050000", mtime=time.time())
    assert discover_run(d).base == new                      # newest by sidecar mtime
    assert discover_run(d, run="cam-20260827-041627").base == old   # pinned
    try:
        discover_run(d, run="cam-20990101-000000")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "runs present" in str(e)


def test_discover_run_glob_does_not_cross_runs():
    # `<base>.N` collision-suffixed runs must not leak their parts into `<base>`'s glob
    d = tempfile.mkdtemp()
    base = _make_run(d, "cam-20260827-041627", parts=1, mtime=time.time() - 100)
    _make_run(d, "cam-20260827-041627.1", parts=3, mtime=time.time() - 50)
    assert [os.path.basename(p) for p in discover_run(base).mkv_paths] == \
        ["cam-20260827-041627-00000.mkv"]


def test_discover_run_errors_are_legible():
    d = tempfile.mkdtemp()
    for path, needle in ((d, "contains no runs"), (os.path.join(d, "nope"), "does not exist"),
                         ("", "replay.path is empty")):
        try:
            discover_run(path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert needle in str(e), f"{needle!r} not in {e}"
    base = _make_run(d, "r1")
    os.remove(base + ".csv")
    try:
        discover_run(base)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "sidecar CSV" in str(e)
    base2 = _make_run(d, "r2")
    for p in discover_run(base2).mkv_paths:
        os.remove(p)
    try:
        discover_run(base2)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "recording enabled" in str(e)


def test_load_stamps_reconstructs_verbatim():
    d = tempfile.mkdtemp()
    base = _make_run(d, "run")
    stamps = load_stamps(base + ".csv")
    assert [s.frame_id for s in stamps] == [10, 11, 13]
    assert stamps[0].chunk_ns is None and stamps[1].chunk_ns == 1040
    assert stamps[0].source is TimestampSource.CAMERA
    assert stamps[1].source is TimestampSource.PTP_CHUNK
    assert stamps[2].source is TimestampSource.SYSTEM     # unknown provenance -> system
    assert (stamps[1].timestamp_ns, stamps[1].camera_ns, stamps[1].system_ns) == (1040, 1041, 1045)


def test_median_interval_robust_to_gaps():
    assert median_interval_ns([0, 10, 20, 30, 1000]) == 10   # the gap doesn't skew it
    assert median_interval_ns([5]) == 33_333_333             # underivable -> ~30fps fallback
    assert median_interval_ns([]) == 33_333_333


def test_shift_stamp_shifts_every_time_field():
    st = FrameStamp(frame_id=7, timestamp_ns=100, source=TimestampSource.PTP_CHUNK,
                    system_ns=105, camera_ns=101, chunk_ns=100)
    sh = shift_stamp(st, 1000)
    assert (sh.timestamp_ns, sh.system_ns, sh.camera_ns, sh.chunk_ns) == (1100, 1105, 1101, 1100)
    assert sh.frame_id == 7 and sh.source is TimestampSource.PTP_CHUNK
    none_chunk = shift_stamp(FrameStamp(1, 2, TimestampSource.SYSTEM, 3, 4, None), 10)
    assert none_chunk.chunk_ns is None
    assert shift_stamp(st, 0) is st            # no-op fast path


def test_pacer_speed_zero_never_sleeps():
    p = Pacer(0)
    t0 = time.monotonic()
    for ts in range(0, 10_000_000_000, 1_000_000_000):    # 10 "seconds" of data
        p.wait(ts)
    assert time.monotonic() - t0 < 0.1


def test_pacer_paces_relative_to_first_frame():
    p = Pacer(1000.0)   # 1000x so 3 "seconds" of data pace in ~3ms
    t0 = time.monotonic()
    for ts in (0, 1_000_000_000, 2_000_000_000, 3_000_000_000):
        p.wait(ts)
    dt = time.monotonic() - t0
    assert 0.002 < dt < 0.5, dt


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
