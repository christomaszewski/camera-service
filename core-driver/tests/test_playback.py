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



# ---- sessions: every recording of a run, in timeline order --------------------------------------

def _fake_session(d, prefix, first_ts_ns, n=3, interval_ns=40_000_000):
    import json
    base = d / prefix
    hdr = {"pixel_format": "GRAY8", "width": 8, "height": 8, "first_timestamp_ns": first_ts_ns,
           "cfa_tile_mode": "off", "bayer_pattern": None}
    (base.with_suffix(".json")).write_text(json.dumps(hdr))
    rows = ["frame_id,pts_ns,timestamp_ns,source,chunk_ns,camera_ns,system_ns"]
    for i in range(n):
        ts = first_ts_ns + i * interval_ns
        rows.append(f"{i},{i * interval_ns},{ts},system,,{ts},{ts}")
    (base.with_suffix(".csv")).write_text("\n".join(rows) + "\n")
    (d / f"{prefix}-00000.mkv").write_bytes(b"x")
    return str(base)


def test_discover_sessions_orders_by_first_stamp_not_by_name_or_mtime(tmp_path=None):
    import tempfile
    from pathlib import Path
    from cam_driver.playback import discover_run, discover_sessions
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        late = _fake_session(d, "cam-b-20260907-120000", 5_000_000_000_000)   # written FIRST, later data
        early = _fake_session(d, "cam-a-20260907-110000", 1_000_000_000_000)
        (d / "manifest.json").write_text("{}")                               # no csv: not a session
        got = discover_sessions(str(d))
        assert [r.base for r in got] == [early, late]
        assert got[0].mkv_paths == [str(d / "cam-a-20260907-110000-00000.mkv")]
        assert discover_sessions(str(d), run="cam-b-20260907-120000")[0].base == late   # pinned
        assert discover_sessions(early + ".json")[0].base == early                      # prefix path
        assert discover_run(str(d)).base == late                                        # the old API: newest
        try:
            discover_sessions(str(d), run="nope")
        except ValueError as e:
            assert "nope" in str(e) and "cam-a" in str(e)
        else:
            raise AssertionError("a missing pinned run must be a legible error")


# ---- Pacer: an explicit timeline zero, and a reset for restarts -----------------------------------

def test_pacer_anchor_releases_frame_zero_at_its_place_on_the_timeline():
    p = Pacer(1.0, anchor_src_ns=0)
    t = time.monotonic_ns()
    p.wait(80_000_000)            # first frame sits 80 ms after the zero -> ~80 ms sleep, not 0
    assert 0.05 < (time.monotonic_ns() - t) / 1e9 < 0.3


def test_pacer_reset_forgets_the_old_baseline_so_a_restart_starts_now():
    p = Pacer(1.0)
    p.wait(0)
    p.wait(20_000_000)
    time.sleep(0.1)               # a hold: wall time passes, no data
    p.reset()                     # restart from the top: next wait anchors afresh
    t = time.monotonic_ns()
    p.wait(0)                     # frame 0 again -> immediate (the old baseline would say -120 ms: a burst)
    p.wait(50_000_000)            # then 50 ms of data -> ~50 ms
    assert 0.03 < (time.monotonic_ns() - t) / 1e9 < 0.2


# ---- PlaybackState: start paused, the release gate, restart from the end --------------------------

def test_initial_paused_and_the_start_gate_release_it():
    now = time.time()
    pb = PlaybackState("replay", initial_state=PAUSED, start_at_unix_s=now + 0.2, clock=time.time)
    assert pb.state == PAUSED and pb.controls()[0] == "resume"
    seen = []
    pb.add_observer(seen.append)
    time.sleep(0.5)
    assert pb.state == PLAYING and seen and seen[-1]["state"] == PLAYING


def test_a_start_gate_in_the_past_releases_at_once_and_an_operator_outranks_a_pending_one():
    pb = PlaybackState("replay", initial_state=PAUSED, start_at_unix_s=time.time() - 5)
    assert pb.state == PLAYING
    pb2 = PlaybackState("replay", initial_state=PAUSED, start_at_unix_s=time.time() + 0.2)
    pb2.request("resume")
    pb2.request("pause")          # the operator's last word: paused
    time.sleep(0.4)
    assert pb2.state == PAUSED    # the gate did not override it
    pb2.cancel_start_gate()


def test_a_boot_hold_lets_exactly_the_first_frame_through_then_holds():
    pb = PlaybackState("replay", initial_state=PAUSED)
    assert pb.take_preroll() is True and pb.take_preroll() is False    # once
    assert PlaybackState("replay").take_preroll() is False             # playing at boot: no preroll
    cancel = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", pb.wait_if_paused(cancel)))
    t.start(); t.join(0.2)
    assert t.is_alive()                                                # the second frame holds
    cancel.set(); t.join(1.0)


def test_release_from_the_boot_hold_re_anchors_at_the_epoch_not_the_preroll_frame():
    pb = PlaybackState("replay", initial_state=PAUSED, pacer=Pacer(1.0, anchor_src_ns=0))
    pb.request("resume")
    t = time.monotonic_ns()
    pb.pacer.wait(80_000_000)                            # frame 0 is due 80 ms after release
    assert 0.05 < (time.monotonic_ns() - t) / 1e9 < 0.3
    pb.note_frame(80_000_000, cycle_base_ns=0)
    pb.request("pause"); time.sleep(0.1); pb.request("resume")   # a LATER resume rebases (no burst)
    t = time.monotonic_ns()
    pb.pacer.wait(130_000_000)
    assert 0.03 < (time.monotonic_ns() - t) / 1e9 < 0.2


def test_position_moves_through_silence_after_release_but_never_past_the_duration():
    pb = PlaybackState("replay", initial_state=PAUSED, pacer=Pacer(1.0, anchor_src_ns=0), duration_s=1.0)
    assert pb.descriptor()["position_s"] == 0       # held on the (un-noted) preroll frame
    pb.request("resume")                            # released: the timeline starts NOW at the zero
    time.sleep(0.12)
    d = pb.descriptor()
    assert 0.08 <= d["position_s"] <= 0.4           # moving through the silent lead-in from 0
    time.sleep(1.0)
    assert pb.descriptor()["position_s"] == 1.0     # clamped at the duration
    pb.request("pause")
    assert pb.descriptor()["position_s"] == 0       # held: nothing delivered yet
    late = PlaybackState("replay", initial_state=PAUSED, pacer=Pacer(1.0, anchor_src_ns=0),
                         start_at_unix_s=time.time() - 5, duration_s=9.0)
    time.sleep(0.12)
    assert late.state == PLAYING and 0.08 <= late.descriptor()["position_s"] <= 0.4   # past gate: same release


def test_restart_is_the_one_control_left_when_a_restartable_source_finishes():
    calls = []
    pb = PlaybackState("replay", restartable_when_finished=True)
    pb._on_restart = lambda: (calls.append("rebuild"), pb.take_restart())
    pb.mark_finished()
    assert pb.state == FINISHED and pb.controls() == ["restart"]
    assert not pb.request("pause")["ok"]
    r = pb.request("restart")
    assert r["ok"] and r["state"] == PLAYING and calls == ["rebuild"]
    pcap = PlaybackState("pcap")
    pcap.mark_finished()
    assert pcap.controls() == [] and not pcap.request("restart")["ok"]


def test_descriptor_carries_the_timeline_fields_the_source_fills():
    pb = _pb()
    pb.source_path, pb.session, pb.sessions, pb.epoch_unix_ns = "/runs/x/cam-1", 1, 3, 10 ** 18
    d = pb.descriptor()
    assert (d["source_path"], d["session"], d["sessions"], d["epoch_unix_ns"]) == ("/runs/x/cam-1", 1, 3, 10 ** 18)
    pb.note_frame(10 ** 18 + 2_500_000_000, cycle_base_ns=10 ** 18)   # position counts from the epoch
    assert pb.descriptor()["position_s"] == 2.5


def test_discover_sessions_orders_older_headers_by_file_time():
    # Runs recorded before the lifecycle arc have headers without `first_timestamp_ns`: they
    # still replay, ordered by the sidecar's mtime (written at the session's end -- sessions
    # never overlap, so end order IS start order). A header WITH the stamp sorts ahead of any
    # without, by the stamp.
    import json
    import os
    import tempfile
    from pathlib import Path
    from cam_driver.playback import discover_sessions
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        older = {}
        for name, mtime in (("cam-old-b", 2_000), ("cam-old-a", 1_000)):     # names in reverse time order
            base = _fake_session(d, name, 0, n=2)
            hdr = json.loads(Path(base + ".json").read_text()); hdr.pop("first_timestamp_ns")
            Path(base + ".json").write_text(json.dumps(hdr))
            os.utime(base + ".json", (mtime, mtime))
            older[name] = base
        stamped = _fake_session(d, "cam-new", 5_000_000_000_000)
        got = [r.base for r in discover_sessions(str(d))]
        assert got == [older["cam-old-a"], older["cam-old-b"], stamped]


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
