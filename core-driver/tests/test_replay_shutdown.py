"""Real replay/recorder shutdown with a full appsrc and a stalled downstream queue.

The rescue timer only releases a broken implementation so CI can report a failure;
successful shutdown must finish while the downstream pad is still blocked.
"""
import csv
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import pytest

from cam_driver.config import parse_config
from cam_driver.pipeline import CapturePipeline
from test_recording_h264 import collect
from test_replay_source import H, T0, W, _pump, _px, _source, _write_session


@pytest.fixture
def stalled_replay(tmp_path):
    run = tmp_path / "input"
    run.mkdir()
    _write_session(run, "cam-input", 1, T0, 600)
    source = _source(run, speed=5)
    cfg = parse_config({
        "recording": {"encoder": "ffv1", "output_dir": str(tmp_path / "output")},
        "preview": {"enabled": True, "sink": "appsink name=preview emit-signals=true sync=false"},
        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}},
    })
    pipe = CapturePipeline(cfg, source)
    pipe.build()
    preview = []

    def sample(sink):
        preview.append(sink.emit("pull-sample").get_buffer().pts)
        return Gst.FlowReturn.OK

    pipe.pipeline.get_by_name("preview").connect("new-sample", sample)
    pipe.pipeline.set_state(Gst.State.PLAYING)
    assert pipe.activate()["ok"]
    sess = pipe._session
    assert sess._appsrc.get_property("block")
    sess._appsrc.set_property("max-bytes", 2 * W * H)
    queue = sess._pipeline.get_by_name("rec_q")
    queue.set_property("max-size-buffers", 1)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    pad = queue.get_static_pad("src")
    blocked, full = threading.Event(), threading.Event()

    def hold(_pad, _info):
        blocked.set()
        return Gst.PadProbeReturn.OK

    probe = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, hold)
    sess._appsrc.connect("enough-data", lambda *_: full.set())
    release_lock = threading.Lock()

    def release():
        nonlocal probe
        with release_lock:
            if probe is not None:
                pad.remove_probe(probe)
                probe = None

    try:
        pipe._start_source()  # exercises ReplaySource's GstFrame buffer path, not a fake feeder
        _pump(lambda: blocked.is_set() and full.is_set() and sess.lock.locked(), timeout_s=5)
        assert sess.frames > 0 and pipe.drops.enqueue_failures == 0
        yield SimpleNamespace(pipe=pipe, sess=sess, source=source, preview=preview, release=release)
    finally:
        release()
        pipe.shutdown()


def _without_releasing_recorder(replay, operation):
    rescued = threading.Event()

    def rescue():
        rescued.set()
        replay.release()

    timer = threading.Timer(4, rescue)
    timer.start()
    try:
        started = time.monotonic()
        result = operation()
        elapsed = time.monotonic() - started
        assert not rescued.is_set(), "close needed the downstream stall to be released"
        assert elapsed < 2, f"close exceeded its drain budget: {elapsed:.2f}s"
        return result
    finally:
        timer.cancel()
        timer.join()


def _rows(sess):
    with open(sess.path_base + ".csv", newline="") as file:
        return list(csv.DictReader(file))


def _assert_lossless(sess, result):
    assert result["ok"] and not result["session"]["truncated"], result
    decoded = [frame for part in result["session"]["files"] for frame in collect(
        f'filesrc location="{part}" ! matroskademux ! avdec_ffv1 ! video/x-raw,format=GRAY8')]
    rows = _rows(sess)
    assert len(decoded) == len(rows) == sess.frames > 0
    assert decoded == [(int(row["pts_ns"]), _px(1, int(row["frame_id"]))) for row in rows]


@pytest.mark.parametrize("operation", ["deactivate", "request_stop", "shutdown"])
def test_stalled_replay_close_is_bounded_and_cancellation_is_not_a_drop(stalled_replay, monkeypatch, operation):
    replay = stalled_replay
    pipe, sess = replay.pipe, replay.sess
    monkeypatch.setattr("cam_driver.pipeline.SESSION_DRAIN_S", .15)
    accepted = sess.frames
    _without_releasing_recorder(replay, getattr(pipe, operation))
    assert pipe._session is None and sess.closed and sess._pipeline is None
    assert not sess.sidecar._thread.is_alive()
    assert sess.frames == accepted == len(_rows(sess)), "cancelled push must not add a sidecar row"
    assert pipe.drops.enqueue_failures == 0, "intentional cancellation is not an enqueue failure"
    header = json.loads(Path(sess.path_base + ".json").read_text())
    assert header["session"]["truncated"] is True
    assert header["session"]["frames_recorded"] == accepted
    if operation == "deactivate":
        before = len(replay.preview)
        _pump(lambda: len(replay.preview) >= before + 4, timeout_s=5)
        assert not pipe.had_error
        assert pipe.activate()["ok"]
        healthy = pipe._session
        _pump(lambda: healthy.frames >= 4, timeout_s=5)
        _assert_lossless(healthy, pipe.deactivate())
    else:
        assert replay.source._stop_evt.is_set(), "shutdown must join the actual replay reader"


def test_blocked_push_is_cancelled_but_accepted_frames_still_drain(stalled_replay):
    replay = stalled_replay
    sess = replay.sess
    accepted = sess.frames
    _without_releasing_recorder(replay, sess.begin_close)
    assert sess.frames == accepted and not sess.lock.locked()
    # Only now let the queued frames reach the encoder/muxer. EOS must follow all of them.
    replay.release()
    _assert_lossless(sess, replay.pipe.deactivate())
    assert replay.pipe.drops.enqueue_failures == 0
