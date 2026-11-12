#!/usr/bin/env python3
"""Unit tests for idle_gate (pure; no gi/GStreamer needed). Run: python3 test_idle_gate.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from idle_gate import GRACE_S_DEFAULT, IDLE_FPS_DEFAULT, IdleGate, parse_grace_s, parse_idle_fps  # noqa: E402


def _run(gate, t0, seconds, fps=24):
    """Feed frames at `fps` for `seconds`; return how many passed."""
    return sum(gate.allow(t0 + i / fps) for i in range(int(seconds * fps)))


def test_warm_up_passes_every_frame_then_an_empty_room_gets_a_trickle():
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)
    assert _run(g, 0, 10) == 240                      # codec discovery runs at the camera rate
    assert not g.idle(9.99) and g.idle(10)
    assert _run(g, 10, 10) == 10                      # one frame a second
    assert g.dropped == 230


def test_a_viewer_opens_the_gate_at_once_and_a_reconnect_within_the_grace_never_sees_it_close():
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)
    _run(g, 0, 20)
    g.sessions(1, 20.0)
    assert _run(g, 20, 5) == 120                      # full rate from the first frame
    g.sessions(0, 25.0)                               # the viewer leaves...
    assert _run(g, 25, 4) == 96                       # ...and the chain stays warm for the grace
    g.sessions(1, 29.0)                               # the dashboard reconnects
    assert _run(g, 29, 2) == 48
    g.sessions(0, 31.0)
    assert _run(g, 31, 5) == 120 and not g.idle(35.9)
    assert g.idle(36.0) and _run(g, 36, 4) == 4


def test_a_viewer_during_warm_up_then_none_still_gets_the_grace():
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)
    g.allow(0.0)
    g.sessions(1, 1.0)
    g.sessions(0, 9.0)
    assert not g.idle(13.9) and g.idle(14.0)          # 5 s after it left, though warm-up ended at 10


def test_a_gate_that_is_told_when_the_sink_is_ready_holds_nothing_back_before_that():
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)
    g.await_ready()
    assert _run(g, 0, 60) == 1440 and not g.idle(60)  # a slow discovery: far past the fixed warm-up
    g.ready(60.0)
    g.ready(70.0)                                     # only the first report counts
    assert _run(g, 60, 5) == 120 and not g.idle(64.9) # then the grace, as after a viewer
    assert g.idle(65.0) and _run(g, 65, 10) == 10


def test_ready_and_a_departed_viewer_the_later_of_the_two_starts_the_grace():
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)
    g.await_ready()
    g.allow(0.0)
    g.ready(2.0)
    g.sessions(1, 3.0)
    g.sessions(0, 20.0)
    assert not g.idle(24.9) and g.idle(25.0)
    g = IdleGate(idle_fps=1, grace_s=5, warmup_s=10)  # a viewer cannot leave before the sink is ready,
    g.await_ready()                                   # but a resync to zero can be seen before it
    g.allow(0.0)
    g.sessions(1, 1.0)
    g.sessions(0, 2.0)
    assert not g.idle(100.0)
    g.ready(100.0)
    assert not g.idle(104.9) and g.idle(105.0)


def test_idle_rate_zero_stops_everything_and_other_rates_are_honoured():
    g = IdleGate(idle_fps=0, grace_s=0, warmup_s=1)
    assert _run(g, 0, 1) == 24 and _run(g, 1, 10) == 0
    g.sessions(1, 11.0)
    assert g.allow(11.0)
    g = IdleGate(idle_fps=4, grace_s=0, warmup_s=1)
    _run(g, 0, 1)
    assert _run(g, 1, 10) == 40


def test_a_disabled_gate_passes_everything():
    g = IdleGate(enabled=False)
    assert _run(g, 0, 60) == 1440 and g.dropped == 0 and not g.idle(59)


def test_the_count_never_goes_negative_and_resync_is_idempotent():
    g = IdleGate(grace_s=0, warmup_s=0)
    g.allow(0.0)
    g.sessions(-3, 1.0)
    assert g.idle(1.0)
    g.sessions(2, 2.0)
    g.sessions(2, 3.0)
    assert not g.idle(3.0)
    g.sessions(0, 4.0)
    g.sessions(0, 9.0)                                # a later resync does not restart the grace
    assert g.idle(4.0)


def test_settings():
    assert parse_idle_fps(None) == parse_idle_fps("") == parse_idle_fps("fast") == (True, IDLE_FPS_DEFAULT)
    assert parse_idle_fps("0") == (True, 0.0) and parse_idle_fps("2.5") == (True, 2.5)
    assert [parse_idle_fps(v)[0] for v in ("off", "False", "no", "full", "-1")] == [False] * 5
    assert parse_grace_s(None) == parse_grace_s("x") == parse_grace_s("-1") == GRACE_S_DEFAULT
    assert parse_grace_s("0") == 0 and parse_grace_s("12") == 12


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
