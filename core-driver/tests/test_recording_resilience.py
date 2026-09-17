"""Hardware-free live recording failure, concurrency, and resource regressions.

Synthetic source bytes enter the real core, recorder, preview and shm/unixfd
pipelines. Control races use two real Zenoh peers and the GLib dispatcher.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gc
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
import pytest
import zenoh

from cam_driver.config import parse_config
from cam_driver.control_zenoh import ZenohControl
from cam_driver.lifecycle import Lifecycle
from cam_driver.pipeline import CapturePipeline
from cam_driver.timestamps import FrameStamp, TimestampSource
from cam_driver.transport import HEADER_SIZE, unpack_header
from test_recording_h264 import collect
from test_replay_source import _pump
from test_recording_settings import version

W, H, IV = 64, 48, 40_000_000
KEY = "fleet/test/svc/resilience/lifecycle"
Gst.init(None)


@pytest.fixture
def live(tmp_path):
    stop = threading.Event()
    source = SimpleNamespace(encoded_caps=None, encoded_parser=None, finite=False,
                             delivered_frame_rate=25, active_timestamp_source="system",
                             ptp_locked=False, tick_frequency_hz=0,
                             geometry=lambda: (0, 0, W, H), pixel_format=lambda: "Mono8",
                             take_discontinuity=lambda: False, stop=stop.set, close=lambda: None)
    cfg = parse_config({"recording": {"encoder": "ffv1", "output_dir": str(tmp_path)},
                        "preview": {"enabled": True, "sink": "appsink name=preview emit-signals=true sync=false"},
                        "transport": {"plugin_endpoint": {"enabled": True, "socket_path": str(tmp_path / "frames")},
                                      "raw_endpoint": {"enabled": False}}})
    pipe = CapturePipeline(cfg, source)
    pipe.build()
    lc = Lifecycle(pipe, recording_enabled=True)
    preview, streamed, errors = [], [], []

    def preview_sample(sink):
        buf = sink.emit("pull-sample").get_buffer()
        preview.append(buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK

    pipe.pipeline.get_by_name("preview").connect("new-sample", preview_sample)
    pipe.pipeline.set_state(Gst.State.PLAYING)
    endpoint = (f'unixfdsrc socket-path="{pipe._unixfd_path}"' if pipe._have_unixfd else
                f'shmsrc socket-path="{tmp_path}/frames" is-live=true do-timestamp=true')
    consumer = Gst.parse_launch(endpoint + " ! appsink name=sink emit-signals=true sync=false")

    def transport_sample(sink):
        buf = sink.emit("pull-sample").get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        fid = buf.offset
        if not pipe._have_unixfd:
            fid, data = unpack_header(data).frame_id, data[HEADER_SIZE:]
        if data != bytes([fid % 251]) * (W * H):
            errors.append("transport pixels changed")
        streamed.append(fid)
        return Gst.FlowReturn.OK

    consumer.get_by_name("sink").connect("new-sample", transport_sample)
    consumer.set_state(Gst.State.PLAYING)

    def feed():
        fid = 0
        try:
            while not stop.is_set():
                ts = 1_788_000_000_000_000_000 + fid * IV
                pipe._on_frame(FrameStamp(fid, ts, TimestampSource.SYSTEM, ts, ts, None),
                               bytes([fid % 251]) * (W * H))
                fid += 1
                stop.wait(.005)
        except Exception as exc:
            errors.append(str(exc))

    feeder = threading.Thread(target=feed, name="test-live-feeder", daemon=True)
    feeder.start()
    state = SimpleNamespace(pipe=pipe, lc=lc, preview=preview, streamed=streamed, errors=errors)
    try:
        _pump(lambda: len(streamed) >= 5 and len(preview) >= 5, timeout_s=5)
        yield state
        assert not errors, errors
        assert all(len(frame) == W * H and frame == frame[:1] * (W * H) for frame in preview)
    finally:
        stop.set()
        feeder.join(3)
        consumer.set_state(Gst.State.NULL)
        pipe.shutdown()
        assert not feeder.is_alive(), "source shutdown blocked"


def _keeps_streaming(live):
    before = len(live.preview), len(live.streamed)
    _pump(lambda: len(live.preview) >= before[0] + 5 and len(live.streamed) >= before[1] + 5, timeout_s=5)
    assert not live.pipe.had_error


def _healthy_session(live):
    assert live.lc.request("activate")["ok"]
    sess = live.pipe._session
    _pump(lambda: sess.frames >= 4, timeout_s=5)
    result = live.lc.request("deactivate")
    assert result["ok"] and not result["session"]["truncated"], result
    frames = [data for part in result["session"]["files"] for _, data in collect(
        f'filesrc location="{part}" ! matroskademux ! avdec_ffv1 ! video/x-raw,format=GRAY8')]
    assert len(frames) == sess.frames
    return sess


def test_muxer_write_failure_preserves_streaming_and_allows_recording_again(live):
    assert live.lc.request("activate")["ok"]
    sess = live.pipe._session
    _pump(lambda: sess.frames >= 4)
    # Inject the same bus event filesink posts for ENOSPC, after real frames were recorded.
    # This is isolated to the recorder and never fills the host filesystem.
    error = GLib.Error.new_literal(Gst.ResourceError.quark(), "No space left on device", Gst.ResourceError.NO_SPACE_LEFT)
    started = time.monotonic()
    assert sess._pipeline.post_message(Gst.Message.new_error(sess._pipeline, error, "injected ENOSPC"))
    _pump(lambda: live.pipe._session is None, timeout_s=5)
    assert time.monotonic() - started < 5
    assert live.lc.state == "inactive" and live.lc.last_error
    header = json.loads(Path(sess.path_base + ".json").read_text())
    assert header["session"]["truncated"] and "No space" in header["session"]["error"]
    assert Path(sess.settings_path).exists()
    _keeps_streaming(live)
    _healthy_session(live)


def test_sidecar_flush_failure_is_attested_without_stopping_live_frames(live, monkeypatch):
    import cam_driver.sidecar as sidecar
    real_open = open
    failed = threading.Event()

    class FailingCSV:
        def __init__(self, file): self.file = file
        def __enter__(self): return self
        def __exit__(self, *args): self.file.close()
        def write(self, text):
            # Header succeeds, then an actual writer-thread OSError while recording is live.
            if text.startswith("frame_id"):
                return self.file.write(text)
            failed.set()
            raise OSError(28, "No space left on device")
        def flush(self): self.file.flush()

    def open_csv(path, *args, **kwargs):
        f = real_open(path, *args, **kwargs)
        return FailingCSV(f) if str(path).endswith(".csv") else f

    with monkeypatch.context() as patch:
        patch.setattr(sidecar, "open", open_csv, raising=False)
        assert live.lc.request("activate")["ok"]
        sess = live.pipe._session
        _pump(lambda: failed.is_set() and sess.sidecar._failed, timeout_s=5)
        _keeps_streaming(live)
        started = time.monotonic()
        result = live.lc.request("deactivate")
        assert result["ok"] and time.monotonic() - started < 5
    assert not sess.sidecar._thread.is_alive()
    header = json.loads(Path(sess.path_base + ".json").read_text())
    assert header["sidecar_csv_failed"] is True
    _healthy_session(live)


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_snapshot_failure_never_starts_an_unaudited_recording(live, monkeypatch, operation):
    import cam_driver.recording_settings as settings
    def fail(*_args):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(settings.os, operation, fail)
        result = live.lc.request("activate")
    assert not result["ok"] and "No space" in result["error"]
    assert live.pipe._session is None and live.lc.state == "inactive"
    root = Path(live.pipe.cfg.recording.output_dir)
    assert not list(root.glob("*.mkv")) and not list(root.glob("*.recording-settings.json"))
    assert not list(root.glob("*.tmp"))
    _keeps_streaming(live)
    _healthy_session(live)


def test_first_frame_header_write_failure_does_not_kill_capture(live, monkeypatch):
    from cam_driver.sidecar import SidecarWriter
    def fail(*_args):
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(SidecarWriter, "write_header", fail)
        assert live.lc.request("activate")["ok"]
        sess = live.pipe._session
        _pump(lambda: live.pipe._session is None, timeout_s=5)
    assert "No space" in live.lc.last_error
    assert sess.frames == 0 and Path(sess.settings_path).exists()
    header = json.loads(Path(sess.path_base + ".json").read_text())
    assert header["session"]["truncated"] and header["session"]["error"]
    _keeps_streaming(live)
    _healthy_session(live)


def test_stalled_recorder_has_bounded_backpressure_and_live_output_continues(live):
    assert live.lc.request("activate")["ok"]
    sess = live.pipe._session
    queue = sess._pipeline.get_by_name("rec_q")
    queue.set_property("max-size-buffers", 1)
    queue.set_property("max-size-bytes", 0)
    queue.set_property("max-size-time", 0)
    pad = queue.get_static_pad("src")
    blocked = threading.Event()
    def hold(_pad, _info):
        blocked.set()
        return Gst.PadProbeReturn.OK
    probe = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM, hold)
    before = live.pipe.drops.enqueue_failures
    try:
        _pump(lambda: blocked.is_set() and live.pipe.drops.enqueue_failures > before, timeout_s=5)
        limit = sess._appsrc.get_property("max-bytes")
        assert 0 < sess._appsrc.get_property("current-level-bytes") <= limit
        _keeps_streaming(live)
    finally:
        pad.remove_probe(probe)
    # Stop while frames remain queued: all accepted frames must drain, without a hang.
    result = live.lc.request("deactivate")
    assert result["ok"] and not result["session"]["truncated"], result
    decoded = [data for part in result["session"]["files"] for _, data in collect(
        f'filesrc location="{part}" ! matroskademux ! avdec_ffv1 ! video/x-raw,format=GRAY8')]
    assert len(decoded) == sess.frames
    _healthy_session(live)


@contextmanager
def _control(live):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"

    def peer(listen=False):
        cfg = zenoh.Config()
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("listen/endpoints" if listen else "connect/endpoints", json.dumps([endpoint]))
        return zenoh.open(cfg)

    server = peer(True)
    clients = []
    ctl = ZenohControl(live.lc, KEY, dispatch=lambda fn, *args: GLib.idle_add(fn, *args),
                       session_factory=lambda _: server)
    try:
        assert ctl.advertise()
        clients = [peer(), peer()]
        # A descriptor reply proves routing/advertisement, rather than sleeping for discovery.
        for client in clients:
            assert _query(client, "")["recording_settings"]
        yield clients
    finally:
        for client in clients:
            client.close()
        ctl.close()


def _query(client, suffix, body=None):
    kwargs = {"timeout": 3.0}
    if body is not None:
        kwargs["payload"] = json.dumps(body).encode()
    replies = list(client.get(KEY + suffix, **kwargs))
    assert len(replies) == 1 and replies[0].ok, replies
    return json.loads(replies[0].ok.payload.to_bytes())


def _race(clients, requests):
    barrier = threading.Barrier(len(clients))
    def call(client, request):
        barrier.wait(timeout=3)
        return _query(client, *request)
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        futures = [pool.submit(call, c, r) for c, r in zip(clients, requests)]
        _pump(lambda: all(f.done() for f in futures), timeout_s=8)
        return [f.result() for f in futures]


def test_two_zenoh_editors_cannot_overwrite_each_others_revision(live):
    with _control(live) as clients:
        expected = version(live.pipe)
        replies = _race(clients, [("/configure_recording", {"expected": expected, "settings": {"x264_crf": crf}})
                                  for crf in (18, 30)])
        assert sum(r["ok"] for r in replies) == 1
        winner = next(i for i, reply in enumerate(replies) if reply["ok"])
        assert live.pipe.recording_settings()["requested"]["x264_crf"] == (18, 30)[winner]
        assert version(live.pipe)["revision"] == expected["revision"] + 1
        assert not list(Path(live.pipe.cfg.recording.output_dir).glob("*.mkv"))
        _keeps_streaming(live)


def test_zenoh_settings_change_racing_activation_uses_one_consistent_snapshot(live):
    with _control(live) as clients:
        expected = version(live.pipe)
        replies = _race(clients, [
            ("/configure_recording", {"expected": expected, "settings": {"x264_crf": 18}}),
            ("/change_state", {"transition": "activate", "expected_recording_settings": expected}),
        ])
        assert sum(r["ok"] for r in replies) == 1
        if replies[1]["ok"]:
            sess = live.pipe._session
            snapshot = json.loads(Path(sess.settings_path).read_text())
            assert snapshot["revision"] == expected["revision"] and snapshot["requested"]["x264_crf"] == 23
            assert live.lc.request("deactivate")["ok"]
        else:
            assert live.pipe._session is None and version(live.pipe)["revision"] == expected["revision"] + 1
        _keeps_streaming(live)


def test_repeated_sessions_release_resources_and_keep_streaming(live):
    def resources():
        gc.collect()
        # Current RSS, not peak RSS: Python/native allocators get a warmup allowance below.
        rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        return len(list(Path("/proc/self/fd").iterdir())), threading.active_count(), rss

    samples = []
    audits = {}
    for i in range(110):
        assert live.lc.configure_recording({"expected": version(live.pipe), "settings": {
            "encoder": "x264" if i % 2 else "ffv1", "x264_crf": 18 + i % 15}})["ok"]
        assert live.lc.request("activate")["ok"]
        sess = live.pipe._session
        _pump(lambda: sess.frames >= 2, timeout_s=5)
        assert live.lc.request("deactivate")["ok"]
        assert sess._pipeline is None and not sess.sidecar._thread.is_alive()
        audit = Path(sess.settings_path)
        assert audit not in audits, "rapid sessions must never overwrite an existing prefix"
        audits[audit] = audit.read_bytes()
        if i in (9, 59, 109):
            samples.append(resources())
    assert all(path.read_bytes() == data for path, data in audits.items())
    assert samples[-1][0] <= samples[0][0] + 3, samples
    assert samples[-1][1] <= samples[0][1] + 1, samples
    assert samples[-1][2] <= samples[0][2] + 64 * 1024 * 1024, samples
    _keeps_streaming(live)
