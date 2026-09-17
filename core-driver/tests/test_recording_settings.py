"""Runtime validation, Zenoh dispatch, recording locks, audit files, and unchanged raw pixels.

Run in cam-dev; real GStreamer sessions with deterministic frames and an injected Zenoh session.
"""
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from cam_driver.config import RecordingConfig, parse_config
from cam_driver.control_zenoh import ZenohControl
from cam_driver.lifecycle import Lifecycle
from cam_driver.pipeline import CapturePipeline
from cam_driver.playback import _run_names
from cam_driver.recording_settings import apply_patch, requested
from cam_driver.timestamps import FrameStamp, TimestampSource
from test_control_zenoh import _Session, _Query
from test_recording_h264 import collect

Gst.init(None)
W, H, IV, N = 64, 48, 40_000_000, 32
KEY = "fleet/test/svc/camera/lifecycle"


class Source:
    encoded_caps = encoded_parser = None
    delivered_frame_rate = 25
    finite = True
    active_timestamp_source = "system"
    ptp_locked = False
    tick_frequency_hz = 0

    def geometry(self): return 0, 0, W, H
    def pixel_format(self): return "NV12"
    def take_discontinuity(self): return False


def pipe(tmp, source=None):
    cfg = parse_config({"recording": {"encoder": "ffv1", "output_dir": str(tmp)},
                        "preview": {"enabled": True, "sink": "appsink name=preview_sink sync=false"},
                        "transport": {"plugin_endpoint": {"enabled": False},
                                      "raw_endpoint": {"enabled": False}}})
    p = CapturePipeline(cfg, source or Source())
    p.build()
    return p


def version(p):
    v = p.recording_settings()
    return {"generation": v["generation"], "revision": v["revision"]}


def test_validation_never_accepts_paths_injection_or_invalid_numbers():
    cfg = RecordingConfig()
    for changes in ({}, [], {"enabled": True}, {"output_dir": "/tmp/elsewhere"},
                    {"encoder": "x264 ! filesink"}, {"x264_preset": "bad"},
                    {"x264_crf": True}, {"x264_crf": 0}, {"x264_crf": 51}, {"x264_crf": 2.5},
                    {"segment_seconds": 0}, {"keyframe_interval_s": float("nan")},
                    {"videoconvert_threads": -1}, {"nvenc_maxperf": 1}):
        try:
            apply_patch(cfg, changes)
            raise AssertionError(changes)
        except ValueError:
            pass
    assert cfg == RecordingConfig()
    assert apply_patch(cfg, {"encoder": "x264", "x264_crf": 28}).encoder == "x264"


def test_control_round_trip_and_sessions_keep_pixels_and_frozen_settings():
    caps = f"video/x-raw,format=NV12,width={W},height={H},framerate=25/1"
    frames = [b for _, b in collect(f"videotestsrc num-buffers={N} pattern=smpte ! {caps}")]
    with tempfile.TemporaryDirectory() as tmp:
        p = pipe(tmp)
        lc = Lifecycle(p, recording_enabled=True)
        bus, pending = _Session([]), []
        ctl = ZenohControl(lc, KEY, dispatch=lambda fn, *args: pending.append((fn, args)),
                           session_factory=lambda _: bus)
        assert ctl.advertise()
        def call(suffix, body):
            query = _Query(KEY + suffix, json.dumps(body).encode())
            bus.queryables[KEY + suffix][0](query)
            assert not query.replies, "mutations must run on the main loop, not a Zenoh thread"
            fn, args = pending.pop(0)
            assert fn(*args) is False
            return query.replies[0][1]

        p.pipeline.set_state(Gst.State.PLAYING)
        preview = p.pipeline.get_by_name("preview_sink")
        try:
            before = version(p)
            result = call("/configure_recording", {"expected": before, "settings": {
                "encoder": "x264", "x264_crf": 28, "segment_seconds": 1, "keyframe_interval_s": 1}})
            assert result["ok"] and result["descriptor"]["recording_settings"]["resolved"]["encoder"] == "x264"
            assert p.recording_settings()["revision"] == 1
            assert not call("/configure_recording", {"expected": before, "settings": {"x264_crf": 18}})["ok"]
            assert not call("/change_state", {"transition": "activate", "expected_recording_settings": before})["ok"]
            assert not list(Path(tmp).glob("*")), "stale activation must open nothing"

            snapshot_bytes = []
            for iteration, encoder in enumerate(("x264", "ffv1")):
                if iteration:
                    assert call("/configure_recording", {"expected": version(p), "settings": {"encoder": encoder}})["ok"]
                assert call("/change_state", {"transition": "activate", "expected_recording_settings": version(p)})["ok"]
                sess = p._session
                path = Path(sess.settings_path)
                assert path.exists(), "settings must exist even before the first frame"
                snapshot_bytes.append((path, path.read_bytes()))
                audit = json.loads(path.read_text())
                assert audit["requested"]["encoder"] == audit["resolved"]["encoder"] == encoder
                assert audit["requested"]["x264_crf"] == 28
                assert audit["requested"]["output_dir"] == tmp
                assert audit["revision"] == iteration + 1
                assert lc.descriptor()["recording"]["segment_seconds"] == 1
                assert lc.descriptor()["recording"]["settings_file"] == str(path)
                assert not p.recording_settings()["editable"]
                denied = call("/configure_recording", {"expected": version(p), "settings": {"x264_crf": 18}})
                assert not denied["ok"] and "locked" in denied["error"]
                for i, frame in enumerate(frames):
                    fid = iteration * N + i
                    ts = 10**18 + fid * IV
                    stamp = FrameStamp(frame_id=fid, timestamp_ns=ts, source=TimestampSource.SYSTEM,
                                       chunk_ns=None, camera_ns=ts, system_ns=ts)
                    p._on_frame(stamp, frame)
                    sample = preview.emit("try-pull-sample", 2 * Gst.SECOND)
                    assert sample is not None
                    buf = sample.get_buffer()
                    assert buf.extract_dup(0, buf.get_size()) == frame, "record settings must not change preview pixels"
                result = call("/change_state", {"transition": "deactivate"})
                assert result["ok"] and not result["session"]["truncated"], result
                assert result["session"]["frames"] == N
                assert p.recording_settings()["editable"]
                header = json.loads(Path(sess.path_base + ".json").read_text())
                assert header["recording_settings_file"] == str(path)
                assert header["recording_encoder"] == encoder
                decoder = "h264parse ! avdec_h264" if encoder == "x264" else "avdec_ffv1"
                decoded = [item for part in result["session"]["files"]
                           for item in collect(f'filesrc location="{part}" ! matroskademux ! {decoder} ! videoconvert ! {caps}')]
                rows = list(csv.DictReader(Path(sess.path_base + ".csv").open()))
                assert len(decoded) == len(rows) == N
                assert all(abs(pts - int(row["pts_ns"])) <= 1_000_000 for (pts, _), row in zip(decoded, rows))
                if encoder == "ffv1":
                    assert [b for _, b in decoded] == frames
            assert all(path.read_bytes() == data for path, data in snapshot_bytes)
            assert len(_run_names(tmp)) == 2, "settings JSON must not be discovered as a replay session"
            assert p.drops.enqueue_failures == p.drops.frames_missing == 0
            assert bus.publisher.puts, "settings updates publish the lifecycle descriptor"
        finally:
            if p._session:
                p.deactivate()
            ctl.close()
            p.pipeline.set_state(Gst.State.NULL)


def test_refusals_are_atomic_and_lock_covers_transitions_and_stop():
    with tempfile.TemporaryDirectory() as tmp:
        p = pipe(tmp)
        lc = Lifecycle(p, recording_enabled=True)
        initial = p.recording_settings()
        for state in ("active", "activating", "deactivating"):
            p._lifecycle = state
            assert not p.recording_settings()["editable"]
            assert not lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})["ok"]
        p._lifecycle = "inactive"
        p._stopping = True
        assert not lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})["ok"]
        p._stopping = False
        with patch("cam_driver.pipeline.Gst.parse_launch", side_effect=RuntimeError("missing plugin")):
            result = lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})
        assert not result["ok"] and p.recording_settings() == initial
        with patch("cam_driver.pipeline.settings.write_snapshot", side_effect=OSError("disk full")):
            result = lc.request("activate")
        assert not result["ok"] and "disk full" in result["error"]
        assert p._session is None and p._lifecycle == "inactive"
        assert not list(Path(tmp).glob("*.mkv"))
        # A zero-frame session still carries the requested settings and a summary reference.
        assert lc.request("activate")["ok"]
        path = Path(p._session.settings_path)
        assert json.loads(path.read_text())["requested"] == requested(p.cfg.recording)
        result = lc.request("deactivate")
        assert result["session"]["frames"] == 0 and result["session"]["settings_file"] == str(path)


def test_encoded_feed_mode_cannot_change_at_runtime():
    with tempfile.TemporaryDirectory() as tmp:
        source = Source()
        source.encoded_caps, source.encoded_parser = "image/jpeg", "jpegparse"
        p = pipe(tmp, source)
        lc = Lifecycle(p, recording_enabled=True)
        before = p.recording_settings()
        result = lc.configure_recording({"expected": version(p), "settings": {"encoder": "stream-copy"}})
        assert not result["ok"] and "restart" in result["error"]
        assert p.recording_settings() == before
        assert lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})["ok"]
        p.cfg.recording.encoder = "auto"
        p._install_recording_plan(p.cfg.recording, p._recording_plan(p.cfg.recording))
        assert p._stream_copy
        assert lc.configure_recording({"expected": version(p), "settings": {"segment_seconds": 5}})["ok"]
        assert not lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})["ok"]
        assert p._stream_copy


def test_restart_versions_legacy_aliases_and_depth_fallback_are_visible():
    with tempfile.TemporaryDirectory() as tmp:
        first = pipe(tmp)
        source = Source()
        source.pixel_format = lambda: "Mono16"
        p = pipe(tmp, source)
        lc = Lifecycle(p, recording_enabled=True)
        assert not lc.configure_recording({"expected": version(first), "settings": {"encoder": "x264"}})["ok"]
        initial = version(p)
        for expected in ({}, None, {**initial, "revision": True}, {**initial, "revision": .0}):
            assert not lc.configure_recording({"expected": expected, "settings": {"encoder": "x264"}})["ok"]
        assert lc.configure_recording({"expected": initial, "settings": {"encoder": "ffv1"}})["noop"]
        assert version(p) == initial
        p.cfg.recording.bayer_tile = True
        p.cfg.recording.nvenc_preset = 3
        p._install_recording_plan(p.cfg.recording, p._recording_plan(p.cfg.recording))
        assert p.recording_settings()["requested"]["bayer_tile"] == "plain"
        assert p.recording_settings()["requested"]["nvenc_preset"] == "medium"
        assert requested(p.cfg.recording)["bayer_tile"] is True, "the audit retains the YAML request"
        # The existing depth guard still wins over an explicitly requested lossy encoder.
        result = lc.configure_recording({"expected": version(p), "settings": {"encoder": "x264"}})
        assert result["ok"]
        view = result["descriptor"]["recording_settings"]
        assert view["requested"]["encoder"] == "x264"
        assert view["resolved"]["encoder"] == "ffv1" and view["resolved"]["lossy"] is False
        assert p._tiler is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name, flush=True)
