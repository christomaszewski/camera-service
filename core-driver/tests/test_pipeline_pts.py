"""Tests for CapturePipeline._pts_for -- the single owner of the recorded PTS timeline.

This is the piece that decides what timestamp every recorded frame carries, and it had been covered
only by end-to-end smokes, which exercise exactly one shape: a healthy source with monotonic stamps.
Every defect it was written to fix lives in the OTHER shapes -- a duplicate stamp from a re-timing
decode branch, a camera clock that steps backward across a reconnect, a clock-SOURCE change that
steps hours forward, and a rebuilt GigE stream whose block ids restart at 1.

pipeline.py imports gi at module scope, so this SKIPs on a bare host (the dev container and CI both
have the bindings). No GStreamer objects are constructed -- _pts_for is pure state arithmetic.

Run: python3 core-driver/tests/test_pipeline_pts.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cam_driver.pipeline import CapturePipeline, _MAX_PTS_JUMP_NS
    from cam_driver.timestamps import FrameStamp, TimestampSource
except (ImportError, ValueError) as e:   # no gi/GStreamer on this host
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(f"pipeline needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")
    sys.exit(0)

FPS = 30
INTERVAL = 1_000_000_000 // FPS


def _pipe(interval_ns=INTERVAL):
    """A CapturePipeline with only the collaborators _pts_for touches."""
    source = SimpleNamespace(
        geometry=lambda: (0, 0, 8, 8), pixel_format=lambda: "Mono8",
        active_timestamp_source="ptp_chunk", ptp_locked=True, tick_frequency_hz=1_000_000_000)
    cfg = SimpleNamespace(recording=SimpleNamespace(bayer_pattern=None))
    p = CapturePipeline(cfg, source)
    p._frame_interval_ns = interval_ns
    return p


def _stamp(fid, ts):
    return FrameStamp(frame_id=fid, timestamp_ns=ts, source=TimestampSource.PTP_CHUNK,
                      system_ns=ts, camera_ns=ts, chunk_ns=ts)


def test_first_frame_defines_the_base():
    # The sidecar header is written by the recording SESSION on its first recorded frame
    # (test_session.py); _pts_for only owns the time base.
    p = _pipe()
    assert p._pts_for(_stamp(1, 5_000)) == 0
    assert p._base_ts == 5_000


def test_steady_stream_is_the_plain_difference():
    p = _pipe()
    base = 1_000_000_000
    for i in range(10):
        assert p._pts_for(_stamp(i, base + i * INTERVAL)) == i * INTERVAL
    assert p.drops.pts_rebases == 0


def test_same_frame_id_always_returns_the_same_pts():
    # The memo is what makes the recording (_on_encoded) and the consumer path (_on_frame) agree:
    # a stream-copy source hands BOTH callbacks the same FrameStamp for a frame.
    p = _pipe()
    s = _stamp(7, 2_000_000_000)
    assert p._pts_for(s) == p._pts_for(s)
    assert p.drops.pts_rebases == 0        # a second look must not read as a discontinuity


def test_duplicate_stamp_does_not_shift_the_timeline():
    # THE #46 defect. A decode-branch parser re-times buffers (jpegparse does this to MJPEG), so
    # _stamp_for misses and reuses the newest stamp -- two callbacks in a row see the same one. That
    # used to rebase _base_ts, permanently shifting every RECORDED frame by one interval, cumulatively.
    p = _pipe()
    base = 1_000_000_000
    p._pts_for(_stamp(0, base))
    dup = _stamp(1, base + INTERVAL)
    assert p._pts_for(dup) == INTERVAL
    assert p._pts_for(dup) == INTERVAL     # same frame again: memo, not a rebase
    assert p._base_ts == base, "the time base must not move for a repeated frame"
    # ...and the NEXT genuine frame still lands where the wall clock says it should.
    assert p._pts_for(_stamp(2, base + 2 * INTERVAL)) == 2 * INTERVAL
    assert p.drops.pts_rebases == 0


def test_backward_stamp_is_rebased_and_counted():
    p = _pipe()
    base = 1_000_000_000
    p._pts_for(_stamp(0, base))
    p._pts_for(_stamp(1, base + INTERVAL))
    back = p._pts_for(_stamp(2, base - 500_000_000))    # camera clock reset
    assert back == 2 * INTERVAL, "a backward stamp must continue the timeline, not go negative"
    assert p.drops.pts_rebases == 1
    # The skew is permanent, so later frames stay continuous instead of jumping back forward.
    assert p._pts_for(_stamp(3, base - 500_000_000 + INTERVAL)) == 3 * INTERVAL


def test_clock_source_change_is_absorbed():
    # RTSP reopen() drops the NTP anchor: frames carry host arrival time until the first RTCP SR,
    # then jump to the camera's wall clock. Unabsorbed that lands in splitmuxsink's max-size-time and
    # it cuts a file per keyframe until the disk fills.
    p = _pipe()
    base = 1_000_000_000
    p._pts_for(_stamp(0, base))
    jumped = p._pts_for(_stamp(1, base + _MAX_PTS_JUMP_NS + 60_000_000_000))
    assert jumped == INTERVAL
    assert p.drops.pts_rebases == 1


def test_a_real_outage_stays_visible():
    # The counterpart: a reconnect gap SHORTER than the epoch-change threshold is real elapsed time
    # and must survive into the recording. rtsp_reconnect_test.sh depends on this (18.4s stall).
    p = _pipe()
    base = 1_000_000_000
    p._pts_for(_stamp(0, base))
    gap = 18_400_000_000
    assert p._pts_for(_stamp(1, base + gap)) == gap
    assert p.drops.pts_rebases == 0


def test_negative_pts_is_clamped():
    p = _pipe()
    p._base_ts = 10_000_000_000          # pre-set a base ahead of the first stamp
    assert p._pts_for(_stamp(0, 1_000)) == 0


def test_memo_is_bounded():
    from cam_driver.pipeline import _PTS_MEMO_FRAMES
    p = _pipe()
    base = 1_000_000_000
    for i in range(_PTS_MEMO_FRAMES + 50):
        p._pts_for(_stamp(i, base + i * INTERVAL))
    assert len(p._pts_memo) <= _PTS_MEMO_FRAMES


def test_recycled_frame_id_after_a_memo_clear_does_not_return_a_stale_pts():
    # A rebuilt GigE stream restarts the GVSP block id at 1. If the memo still held those ids from
    # before the flap, the hit would return the OLD frame's pts -- and a memo hit returns BEFORE the
    # monotonicity guard, so a backward PTS would reach the muxer unchecked. _reconnect clears it.
    p = _pipe()
    base = 1_000_000_000
    for i in range(1, 6):
        p._pts_for(_stamp(i, base + i * INTERVAL))
    last = p._last_pts

    p._pts_memo.clear()                   # what _reconnect does after a successful reopen()

    recycled = p._pts_for(_stamp(1, base + 100 * INTERVAL))
    assert recycled > last, "a recycled frame_id must be re-derived, not served from the memo"


def test_recycled_frame_id_with_a_new_stamp_is_never_served_from_the_memo():
    # The memo is keyed on (frame_id, timestamp_ns), not frame_id alone. Ids recycle WITHOUT a
    # reconnect: a playback source looping a run repeats them every cycle, and a memo hit returns
    # BEFORE the monotonicity guard -- keyed on the id alone, cycle N+1 would replay cycle N's pts
    # straight into the muxer for any cycle shorter than the memo depth. No clear involved here.
    p = _pipe()
    base = 1_000_000_000
    for i in range(1, 6):
        p._pts_for(_stamp(i, base + i * INTERVAL))
    last = p._last_pts
    again = p._pts_for(_stamp(1, base + 100 * INTERVAL))   # same id, later stamp, memo NOT cleared
    assert again > last, "a recycled id with a new stamp is a new frame, not a memo hit"
    assert p.drops.pts_rebases == 0     # ...and a later stamp is simply later, not a discontinuity


def test_best_effort_branch_never_moves_the_timeline():
    # Stream-copy: the decode branch (_on_frame, own=False) only reads; the encoded branch owns.
    # Its _stamp_for miss can hand it the NEWEST pre-tee stamp before the recording has delivered
    # that frame -- so it must neither define the base, advance _last_pts, nor rebase.
    p = _pipe()
    base = 1_000_000_000
    assert p._pts_for(_stamp(0, base), own=False) == 0
    assert p._base_ts is None and p._last_pts is None and not p._pts_memo
    p._pts_for(_stamp(0, base))                         # the recording branch defines the base
    p._pts_for(_stamp(1, base + INTERVAL))
    ahead = _stamp(2, base + 2 * INTERVAL)               # decode branch runs ahead of the recording
    assert p._pts_for(ahead, own=False) == 2 * INTERVAL
    assert p._pts_for(ahead, own=False) == 2 * INTERVAL  # a duplicate on that branch: no rebase
    assert p._last_pts == INTERVAL and (2, ahead.timestamp_ns) not in p._pts_memo
    assert p.drops.pts_rebases == 0 and p._pts_skew == 0
    assert p._pts_for(ahead) == 2 * INTERVAL             # the recording branch, arriving later, agrees
    assert p._last_pts == 2 * INTERVAL


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
