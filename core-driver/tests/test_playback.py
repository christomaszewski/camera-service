"""Playback control policy + pacer runtime changes (docs/PLAYBACK.md) -- pure Python, no GStreamer.
Run: python3 tests/test_playback.py"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cam_driver.playback import (FINISHED, PAUSED, PLAYING, RESTART, RESUME, STOP,  # noqa: E402
                                 Pacer, PlaybackState)


# ---- Pacer: runtime changes re-anchor the timeline ----------------------------------------

def test_pacer_set_speed_rebases_so_the_new_pace_applies_from_now():
    p = Pacer(1.0)
    p.wait(0)                     # baseline at (0, now)
    t0 = time.monotonic_ns()
    p.wait(50_000_000)            # 50 ms of data at 1x -> ~50 ms sleep
    assert 0.03 < (time.monotonic_ns() - t0) / 1e9 < 0.2
    p.set_speed(10.0)             # rebase at (50 ms, now)
    t1 = time.monotonic_ns()
    p.wait(550_000_000)           # 500 ms of data at 10x -> ~50 ms, NOT 500 ms
    assert (time.monotonic_ns() - t1) / 1e9 < 0.2


def test_pacer_rebase_after_a_pause_does_not_burst():
    p = Pacer(1.0)
    p.wait(0)
    p.wait(20_000_000)
    time.sleep(0.15)              # "paused" 150 ms of wall time with no data flowing
    p.rebase()                    # resume: anchor at (20 ms, now)
    t = time.monotonic_ns()
    p.wait(70_000_000)            # 50 ms of data -> ~50 ms sleep, not 0 (would be a burst)
    assert 0.03 < (time.monotonic_ns() - t) / 1e9 < 0.2


def test_pacer_speed_zero_never_sleeps():
    p = Pacer(0)
    t = time.monotonic_ns()
    for ts in (0, 10**9, 5 * 10**9):
        p.wait(ts)
    assert (time.monotonic_ns() - t) / 1e9 < 0.05


# ---- PlaybackState: policy ------------------------------------------------------------------

def _pb(**kw):
    kw.setdefault("clock", lambda: 1000.0)
    return PlaybackState("pcap", speed=1.0, loop=True, duration_s=4.0, **kw)


def test_boot_descriptor_and_controls():
    pb = _pb()
    d = pb.descriptor()
    assert d["state"] == PLAYING and d["source"] == "pcap" and d["speed"] == 1.0 and d["loop"] is True
    assert d["controls"] == ["pause", "set_speed", "set_loop", "restart"]
    assert d["cycle"] == 0 and d["frames"] == 0 and d["position_s"] == 0 and d["duration_s"] == 4.0
    assert d["since_unix_s"] == 1000.0 and d["last_error"] is None
    assert d["instance"] is None                  # the adapter fills the key's segment


def test_pause_resume_are_idempotent_and_flip_controls():
    pb = _pb()
    seen = []
    pb.add_observer(seen.append)
    r = pb.request("pause")
    assert r["ok"] and r["state"] == PAUSED and "noop" not in r
    assert pb.controls() == ["resume", "set_speed", "set_loop", "restart"]
    assert pb.request("pause") == {**pb.request("pause"), "noop": True}     # idempotent
    r = pb.request("resume")
    assert r["ok"] and r["state"] == PLAYING and pb.request("resume")["noop"] is True
    assert [d["state"] for d in seen] == [PAUSED, PLAYING]                  # noops don't notify


def test_set_speed_validates_and_reaches_the_pacer():
    pb = _pb()
    assert pb.request("set_speed", {"speed": 4}) ["ok"] and pb.pacer.speed == 4.0
    assert pb.request("set_speed", {"speed": 4})["noop"] is True
    assert pb.request("set_speed", {"speed": 0})["ok"] and pb.speed == 0.0          # drain mode
    for bad in ({"speed": -1}, {"speed": "fast"}, {}):
        r = pb.request("set_speed", bad)
        assert not r["ok"] and "speed" in r["error"]


def test_set_loop_validates():
    pb = _pb()
    assert pb.request("set_loop", {"loop": False})["ok"] and pb.loop is False
    assert pb.request("set_loop", {"loop": False})["noop"] is True
    r = pb.request("set_loop", {"loop": "yes"})
    assert not r["ok"] and "loop" in r["error"]


def test_restart_with_a_hook_that_takes_it_replies_immediately():
    # replay's shape: the hook IS the mechanism (a seek) and consumes the flag itself
    calls = []
    pb = _pb()
    pb._on_restart = lambda: (calls.append("seek"), pb.take_restart())
    t = time.monotonic()
    r = pb.request("restart")
    assert r["ok"] and "pending" not in r and calls == ["seek"] and pb.take_restart() is False
    assert time.monotonic() - t < 0.5                       # did not sit out the bounded wait


def test_restart_without_a_hook_waits_for_the_feeder_to_take_it():
    # pcap's shape: no hook; the feeder polls at its pacing point. The reply waits for that.
    pb = _pb()
    taken = []
    def feeder():
        time.sleep(0.15)
        taken.append(pb.wait_if_paused())                   # RESTART, consumed here
    th = threading.Thread(target=feeder); th.start()
    t = time.monotonic()
    r = pb.request("restart")
    th.join(1.0)
    assert r["ok"] and "pending" not in r and taken == [RESTART]
    assert 0.1 < time.monotonic() - t < 0.9                 # replied when taken, not before, not at the bound
    assert pb.wait_if_paused() == RESUME                    # the flag is GONE: the next cycle plays on
    assert pb.take_restart() is False


def test_restart_nobody_takes_replies_pending_at_the_bound():
    pb = _pb()
    t = time.monotonic()
    r = pb.request("restart")
    assert r["ok"] and r["pending"] is True and time.monotonic() - t >= 0.9
    assert pb.take_restart() is True                        # still there for the feeder


def test_a_restart_verdict_is_consumed_so_the_next_cycle_does_not_restart_again():
    # The bench bug: RESTART left the flag set, every new cycle's first frame restarted again,
    # ~2000 empty cycles per second and a dead feed.
    pb = _pb()
    pb._restart_pending = True
    assert pb.wait_if_paused() == RESTART
    assert pb.wait_if_paused() == RESUME and pb.take_restart() is False


def test_restart_hook_failure_is_a_refusal_not_a_crash():
    def boom():
        raise RuntimeError("seek failed")
    pb = _pb(on_restart=boom)
    r = pb.request("restart")
    assert not r["ok"] and "seek failed" in r["error"] and pb.take_restart() is False
    assert pb.descriptor()["last_error"] == "restart failed: seek failed"


def test_unknown_op_and_finished_are_refusals():
    pb = _pb()
    r = pb.request("seek", {"position_s": 1})
    assert not r["ok"] and "unknown op" in r["error"]
    pb.mark_finished()
    assert pb.state == FINISHED and pb.controls() == []
    for op in ("pause", "resume", "restart", "set_speed"):
        r = pb.request(op, {"speed": 1})
        assert not r["ok"] and "finished" in r["error"]
    pb.mark_finished("decode error")         # a second mark is a no-op (first wins)
    assert pb.descriptor()["last_error"] is None


def test_mark_finished_with_error_is_the_last_error():
    pb = _pb()
    seen = []
    pb.add_observer(seen.append)
    pb.mark_finished("bad capture")
    assert seen[-1]["state"] == FINISHED and seen[-1]["last_error"] == "bad capture"


def test_position_and_frames_follow_the_data_and_reset_per_cycle():
    pb = _pb()
    pb.note_frame(5_000_000_000)             # first note anchors the cycle
    pb.note_frame(5_040_000_000)
    pb.note_frame(5_400_000_000)
    d = pb.descriptor()
    assert d["frames"] == 3 and d["position_s"] == 0.4 and d["cycle"] == 0
    pb.mark_cycle(1)
    d = pb.descriptor()
    assert d["cycle"] == 1 and d["frames"] == 0 and d["position_s"] == 0
    pb.note_frame(9_000_000_000)             # a new anchor: the loop-shifted timestamps
    pb.note_frame(9_100_000_000)
    assert pb.descriptor()["position_s"] == 0.1


def test_wait_if_paused_holds_then_wakes_on_resume_restart_or_stop():
    pb = _pb()
    assert pb.wait_if_paused() == RESUME             # playing: never blocks
    pb.request("pause")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", pb.wait_if_paused()))
    t.start(); t.join(0.3)
    assert t.is_alive() and "r" not in out            # held
    pb.request("resume"); t.join(1.0)
    assert out["r"] == RESUME

    pb.request("pause")
    out.clear()
    t = threading.Thread(target=lambda: out.setdefault("r", pb.wait_if_paused()))
    t.start(); t.join(0.3)
    pb.request("restart"); t.join(1.0)               # a restart wakes a paused feeder
    assert out["r"] == RESTART and pb.take_restart() is False   # the verdict CONSUMED it

    out.clear(); cancel = threading.Event()
    t = threading.Thread(target=lambda: out.setdefault("r", pb.wait_if_paused(cancel)))
    t.start(); t.join(0.3)
    cancel.set(); t.join(1.0)                         # shutdown wins over the hold
    assert out["r"] == STOP


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", repr(exc))
    sys.exit(1 if failures else 0)
