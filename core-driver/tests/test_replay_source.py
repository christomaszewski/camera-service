"""The replay SOURCE end to end over a fabricated run (docs/PLAYBACK.md): real FFV1 parts written by
the recorder's own writer (splitmuxsink) + the sidecar pair per session, the real GStreamer reader,
a fake consumer at the seam the pipeline sits on. Covers what only the bench exercised before:
sessions in timeline order with the gap capped (verbatim under an epoch), the window, the boot
hold that lets one frame out, restart from the end (a rebuilt reader), shape refusal, provenance.

Needs GStreamer (gi): run inside the dev container --
  docker run --rm -v "$PWD:/repo" -w /repo/core-driver cam-dev python3 tests/test_replay_source.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cam_driver.config import parse_config  # noqa: E402
from cam_driver.playback import FINISHED, PAUSED  # noqa: E402
from cam_driver.sources.replay import ReplaySource  # noqa: E402

Gst.init(None)

W, H = 16, 16
IV = 40_000_000            # 25 fps, in ns
T0 = 1_788_000_000_000_000_000   # a plausible unix-ns zero


def _px(sid: int, i: int) -> bytes:
    """One flat GRAY8 frame whose bytes name (session, index) -- so delivery order is checkable."""
    return bytes([(sid * 50 + i * 7) % 256]) * (W * H)


def _write_session(run_dir: Path, prefix: str, sid: int, first_ts_ns: int, n: int, *,
                   width: int = W, height: int = H, frame_id0: int = 0,
                   pixel_format: str = "GRAY8", pixels=None) -> Path:
    """A recorded session: FFV1 part(s) via splitmuxsink (the recorder's writer) + <prefix>.json/.csv."""
    base = run_dir / prefix
    pipeline = Gst.parse_launch(
        f'appsrc name=src is-live=false format=time caps="video/x-raw,format={pixel_format},width={width},'
        f'height={height},framerate=25/1" ! videoconvert ! avenc_ffv1 ! splitmuxsink muxer=matroskamux '
        f'max-size-time=0 location="{base}-%05d.mkv"')
    src = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    for i in range(n):
        data = pixels(i) if pixels is not None else bytes([(sid * 50 + i * 7) % 256]) * (width * height)
        buf = Gst.Buffer.new_wrapped(data)
        buf.pts, buf.duration = i * IV, IV
        assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
    src.emit("end-of-stream")
    msg = pipeline.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    assert msg is not None and msg.type == Gst.MessageType.EOS, msg
    pipeline.set_state(Gst.State.NULL)
    header = {"created_unix_s": 0.0, "base_timestamp_ns": first_ts_ns, "timestamp_source": "system",
              "ptp_synced": False, "pixel_format": pixel_format, "bayer_pattern": None, "bits_per_pixel": 8,
              "width": width, "height": height, "tick_frequency_hz": 0, "cfa_tile_mode": "off",
              "session_index": sid, "session_prefix": prefix, "first_pts_ns": 0,
              "first_frame_id": frame_id0, "first_timestamp_ns": first_ts_ns}
    (run_dir / f"{prefix}.json").write_text(json.dumps(header))
    rows = ["frame_id,pts_ns,timestamp_ns,source,chunk_ns,camera_ns,system_ns"]
    for i in range(n):
        ts = first_ts_ns + i * IV
        rows.append(f"{frame_id0 + i},{i * IV},{ts},system,,{ts},{ts}")
    (run_dir / f"{prefix}.csv").write_text("\n".join(rows) + "\n")
    return base


class _Consumer:
    """The pipeline's on_frame, faked: every delivery with its wall time."""

    def __init__(self):
        self.frames = []
        self._lock = threading.Lock()

    def on_frame(self, st, data):
        with self._lock:
            self.frames.append((st, bytes(data), time.monotonic()))

    def count(self) -> int:
        with self._lock:
            return len(self.frames)

    def ids(self):
        return [st.frame_id for st, _, _ in self.frames]

    def stamps(self):
        return [st.timestamp_ns for st, _, _ in self.frames]


def _source(run_dir: Path, *, speed=0.0, gap_max_s=0.3, retime="original", run="", loop=False, **pb):
    cfg = parse_config({"camera": {"type": "replay"},
                        "replay": {"path": str(run_dir), "speed": speed, "gap_max_s": gap_max_s,
                                   "retime": retime, "run": run, "loop": loop},
                        "playback": pb, "control": {"enabled": False}})
    src = ReplaySource(cfg.replay, cfg.playback)
    src.open()
    src.configure()
    return src


def _pump(until, timeout_s: float = 20.0) -> None:
    """Run the default GLib context (the reader's bus watch + idle callbacks live there -- in the
    service the main loop does this) until `until()` holds."""
    ctx = GLib.MainContext.default()
    t0 = time.monotonic()
    while not until():
        while ctx.iteration(False):
            pass
        if time.monotonic() - t0 > timeout_s:
            raise AssertionError(f"timed out after {timeout_s}s")
        time.sleep(0.005)


def _run_dir() -> Path:
    return Path(tempfile.mkdtemp()) / "recordings"


def test_sessions_play_in_timeline_order_with_the_recorded_gap_capped():
    run = _run_dir(); run.mkdir()
    a = _write_session(run, "cam-b-named-first", 1, T0, 5)                        # name order != time order
    b = _write_session(run, "cam-a-later", 2, T0 + 5 * IV + 2_000_000_000, 5, frame_id0=40)
    src = _source(run, speed=1.0, gap_max_s=0.3)
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.finished)
    assert c.ids() == [0, 1, 2, 3, 4, 40, 41, 42, 43, 44], c.ids()                # A then B, by first stamp
    assert c.stamps() == [T0 + i * IV for i in range(5)] + [T0 + 5 * IV + 2_000_000_000 + i * IV for i in range(5)]
    assert [d for _, d, _ in c.frames] == [_px(1, i) for i in range(5)] + [_px(2, i) for i in range(5)]  # lossless
    gap_wall = c.frames[5][2] - c.frames[4][2]
    assert 0.25 < gap_wall < 1.5, gap_wall                                        # the 2.04 s gap, capped to 0.3 s
    d = src.playback.descriptor()
    assert d["state"] == FINISHED and d["sessions"] == 2 and d["session"] == 1
    assert d["source_path"] == str(b) and d["controls"] == ["restart"]
    assert abs(d["duration_s"] - 0.66) < 0.01                                      # effective: 2.4 s span − (2.04 − 0.3)
    assert src.take_discontinuity() is True and src.take_discontinuity() is False  # set once, at the handover
    assert src.provenance() == {"replay_of": [str(a), str(b)], "replay_epoch_unix_ns": T0}
    src.stop()


def test_an_epoch_anchors_the_release_and_keeps_the_gap_verbatim():
    run = _run_dir(); run.mkdir()
    _write_session(run, "cam-a", 1, T0, 3)
    _write_session(run, "cam-b", 2, T0 + 3 * IV + 200_000_000, 3, frame_id0=10)
    src = _source(run, speed=1.0, gap_max_s=0.05, epoch_unix_ns=T0 - 500_000_000)
    assert src._cap_ns == 0                                                        # verbatim under an epoch
    assert abs(src.playback.descriptor()["duration_s"] - 0.94) < 0.01              # epoch .. B.last + one interval
    c = _Consumer()
    t_start = time.monotonic()
    src.start(c.on_frame)
    _pump(lambda: src.finished)
    first = c.frames[0][2] - t_start
    assert 0.45 < first < 1.5, first                                               # frame 0 sits 0.5 s after the zero
    gap_wall = c.frames[3][2] - c.frames[2][2]
    assert 0.2 < gap_wall < 0.9, gap_wall                                          # the recorded 0.24 s, not capped
    assert c.ids() == [0, 1, 2, 10, 11, 12]
    src.stop()


def test_the_window_skips_before_from_and_finishes_at_to():
    run = _run_dir(); run.mkdir()
    _write_session(run, "cam-a", 1, T0, 5)
    _write_session(run, "cam-b", 2, T0 + 5 * IV + 1_000_000_000, 5, frame_id0=40)
    src = _source(run, epoch_unix_ns=T0, from_s=0.1, to_s=1.28)                    # speed 0: as fast as it drains
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.finished)
    assert c.ids() == [3, 4, 40, 41], c.ids()      # A: 0/0.04/0.08 s skipped; B: 1.2, 1.24 in, 1.28 = to
    src.stop()
    src2 = _source(run, epoch_unix_ns=T0, from_s=0.5)                              # the whole of A is before from
    assert src2._first_idx == 1
    c2 = _Consumer()
    src2.start(c2.on_frame)
    _pump(lambda: src2.finished)
    assert c2.ids() == [40, 41, 42, 43, 44]
    src2.stop()


def test_a_paused_boot_lets_exactly_one_frame_out_then_holds_until_resume():
    run = _run_dir(); run.mkdir()
    _write_session(run, "cam-a", 1, T0, 6)
    src = _source(run, initial_state="paused")
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: c.count() >= 1, timeout_s=10)
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        _pump(lambda: True)
        time.sleep(0.02)
    assert c.count() == 1 and src.playback.state == PAUSED                         # the preview frame, then the hold
    assert src.playback.descriptor()["position_s"] == 0                           # a preview is not progress
    r = src.playback.request("resume")
    assert r["ok"]
    _pump(lambda: src.finished)
    assert c.ids() == [0, 1, 2, 3, 4, 5]
    src.stop()


def test_restart_from_the_end_rebuilds_the_reader_and_plays_the_run_again():
    run = _run_dir(); run.mkdir()
    _write_session(run, "cam-a", 1, T0, 4)
    src = _source(run)
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.finished)
    assert c.count() == 4 and src.playback.controls() == ["restart"]
    r = src.playback.request("restart")
    assert r["ok"] and r["state"] == "playing" and not src.finished
    _pump(lambda: c.count() >= 8 and src.finished)
    assert c.ids() == [0, 1, 2, 3] * 2
    span = 3 * IV + IV                                                             # last − first + one median interval
    assert c.stamps()[4:] == [t + span for t in c.stamps()[:4]]                   # cycle 1: shifted, monotonic
    assert src.playback.descriptor()["cycle"] == 1
    src.stop()


def test_sessions_of_a_different_shape_are_refused_and_a_pin_plays_one():
    run = _run_dir(); run.mkdir()
    _write_session(run, "cam-a", 1, T0, 3)
    _write_session(run, "cam-b", 2, T0 + 10 * IV, 3, width=32, frame_id0=20)
    try:
        _source(run)
    except ValueError as e:
        assert "shape" in str(e) and "width" in str(e)
    else:
        raise AssertionError("two shapes in one run must be refused")
    src = _source(run, run="cam-b")                                                 # a pin plays that one alone
    assert src.playback.descriptor()["sessions"] == 1 and src.geometry() == (0, 0, 32, 16)
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.finished)
    assert c.ids() == [20, 21, 22]
    src.stop()


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
    print(f"{len([n for n in globals() if n.startswith('test_')]) - failures} passed")
    sys.exit(1 if failures else 0)
