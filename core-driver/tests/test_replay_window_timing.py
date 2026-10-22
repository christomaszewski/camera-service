"""Window pacing from release, including boot preview and restart, with a controlled clock.

Run inside cam-dev: python3 core-driver/tests/test_replay_window_timing.py
"""
import itertools
from pathlib import Path
import tempfile
from unittest.mock import patch

from test_replay_source import T0, _source, _write_session
from cam_driver.playback import shift_stamp


def test_window_paces_from_from_s_on_release_and_restart():
    with tempfile.TemporaryDirectory() as temporary:
        run = Path(temporary)
        _write_session(run, "cam", 1, T0, 6)
        for shared, paused, speed, retime in itertools.product(
                (False, True), (False, True), (1.0, 2.0), ("original", "wall")):
            # The same 50-ms cut, either relative to the camera or to a bag 120 s earlier.
            lead_s = 120 if shared else 0
            src = _source(run, speed=speed, retime=retime,
                          epoch_unix_ns=T0 - lead_s * 10**9 if shared else None,
                          from_s=lead_s + .05, to_s=lead_s + .16,
                          initial_state="paused" if paused else "playing")
            now = [10**9]
            waits = []

            def wait(seconds):
                waits.append(round(seconds, 9))
                now[0] += round(seconds * 10**9)
                return False

            try:
                with patch("cam_driver.playback.time.monotonic_ns", side_effect=lambda: now[0]), \
                        patch.object(src._stop_evt, "wait", side_effect=wait):
                    def frame(index):
                        stamp = src._new_stamp(None)
                        assert stamp == shift_stamp(src._csv_stamps[index], src._offset)
                        return stamp

                    assert not src._deliverable(frame(0))
                    assert not src._deliverable(frame(1))
                    assert waits == []             # discarded frames never consume replay time
                    assert src._deliverable(frame(2))
                    if paused:
                        assert waits == []         # the correct preview appears before release
                        assert src.playback.descriptor()["frames"] == 0
                        now[0] += 20 * 10**9        # startup hold is separate from the replay window
                        assert src.playback.request("resume")["ok"]
                    assert src._deliverable(frame(3))
                    expected = [.07 / speed] if paused else [.03 / speed, .04 / speed]
                    assert waits == expected, (shared, paused, speed, retime, waits)
                    # The descriptor still addresses the original run, not the cut's local zero.
                    assert src.playback.descriptor()["position_s"] == lead_s + .12
                    assert src.playback.descriptor()["epoch_unix_ns"] == T0 - lead_s * 10**9

                    # Restart/loop share this path. Substitute only the reader rebuild so the
                    # controlled feeder can keep checking real stamp/filter/pacer behavior.
                    for reason in ("restart", "loop"):
                        now[0] += 5 * 10**9
                        waits.clear()
                        with patch.object(src, "_pipeline", None), \
                                patch.object(src, "_rebuild", side_effect=src._activate_session):
                            src._restart(reason=reason)
                        assert not src._deliverable(frame(0))
                        assert not src._deliverable(frame(1))
                        assert src._deliverable(frame(2))
                        assert src._deliverable(frame(3))
                        assert waits == [.03 / speed, .04 / speed], (reason, waits)
            finally:
                src.stop()


def test_window_keeps_only_the_remaining_lead_in_to_a_later_session():
    with tempfile.TemporaryDirectory() as temporary:
        run = Path(temporary)
        _write_session(run, "early", 1, T0, 3)
        _write_session(run, "late", 2, T0 + 120 * 10**9, 3, frame_id0=40)
        src = _source(run, speed=2, epoch_unix_ns=T0, from_s=119.5)
        try:
            waits = []
            with patch("cam_driver.playback.time.monotonic_ns", return_value=10**9), \
                    patch.object(src._stop_evt, "wait", side_effect=waits.append):
                stamp = src._new_stamp(None)
                assert stamp.frame_id == 40 and stamp.timestamp_ns == T0 + 120 * 10**9
                assert waits == [.25], waits  # the remaining 0.5 s at 2x, not the full 120 s
        finally:
            src.stop()


if __name__ == "__main__":
    test_window_paces_from_from_s_on_release_and_restart()
    test_window_keeps_only_the_remaining_lead_in_to_a_later_session()
    print("2 passed")
