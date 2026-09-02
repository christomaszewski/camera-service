#!/usr/bin/env python3
"""Unit tests for threadcpu (pure; no gi). Run: python3 core-driver/tests/test_threadcpu.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cam_driver.threadcpu import ThreadCpu, format_top, parse_stat  # noqa: E402


def test_parse_stat_handles_spaces_and_parens_in_comm():
    # utime=300 stime=50 -> 350 ticks; comm carries a space and a paren, as GStreamer names can
    line = "4242 (videoconvert0:s (x)) R 1 1 1 0 -1 4194560 0 0 0 0 300 50 0 0 20 0 5 0 100 0 0"
    assert parse_stat(line) == ("videoconvert0:s (x)", 350)
    assert parse_stat("garbage") is None
    assert parse_stat("1 (short) R 1") is None      # too few fields


def test_tick_reports_deltas_as_percent_of_one_core():
    samples = [
        {1: ("main", 100), 2: ("nvv4l2h265enc0", 100), 3: ("videoconvert0:s", 100), 5: ("idle", 0)},
        # 10 s later at 100 Hz ticks: main +50 (5%), enc +400 (40%), conv +1000 (100%), new thread 4
        {1: ("main", 150), 2: ("nvv4l2h265enc0", 500), 3: ("videoconvert0:s", 1100), 4: ("new", 999),
         5: ("idle", 3)},                            # +3 ticks = 0.3%: counted in total, not listed
    ]
    it = iter(samples)
    tc = ThreadCpu(top=2, sampler=lambda: next(it), clock_hz=100, now=0.0)   # primed at construction
    total, rows = tc.tick(now=10.0)
    assert round(total) == 145
    assert [(c, round(p)) for c, p in rows] == [("videoconvert0:s", 100), ("nvv4l2h265enc0", 40)]
    assert all(c != "new" for c, _ in rows)           # a thread without a baseline is skipped
    tc2 = ThreadCpu(top=9, sampler=lambda: samples[0], clock_hz=100, now=0.0)
    tc2._sample = lambda: samples[1]
    _, rows_all = tc2.tick(now=10.0)
    assert [c for c, _ in rows_all] == ["videoconvert0:s", "nvv4l2h265enc0", "main"]   # 0.3% omitted


def test_no_proc_means_silence_not_failure():
    tc = ThreadCpu(sampler=lambda: {}, clock_hz=100, now=0.0)
    assert tc.tick(now=1.0) is None
    assert format_top(None) == ""
    idle = ThreadCpu(sampler=lambda: {1: ("main", 100)}, clock_hz=100, now=0.0)
    assert idle.tick(now=1.0) == (0.0, [])            # a thread existed but accrued nothing
    assert format_top((0.0, [])) == "cpu=0%"          # ...which is a fact worth one segment


def test_format_is_one_compact_segment():
    assert format_top((145.4, [("videoconvert0:s", 100.2), ("nvv4l2h265enc0", 40.1)])) == \
        "cpu=145% [videoconvert0:s 100% nvv4l2h265enc0 40%]"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
