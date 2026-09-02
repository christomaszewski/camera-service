"""Tests for CapturePipeline's recording-session mechanism: activate / deactivate / get_state, the
feeders' interaction with an open session (raw, CFA-tiled, stream-copy), the recorder-error path,
and the shutdown ordering that finalizes an open session inside the core's stop budget.

The session itself is a stub (test_session.py covers the real one); the main pipeline's appsrcs are
fakes. pipeline.py imports gi at module scope, so this SKIPs on a bare host. No GStreamer objects are
built -- the buffer constructor is monkeypatched.

Run: python3 core-driver/tests/test_pipeline_session.py
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import cam_driver.pipeline as pipeline_mod
    from cam_driver.lifecycle import ACTIVE, DEACTIVATING, INACTIVE
    from cam_driver.pipeline import CapturePipeline, _RECONNECT_JOIN_S
    from cam_driver.session import SESSION_DRAIN_S, PushResult
    from cam_driver.timestamps import FrameStamp, TimestampSource
    from gi.repository import GLib, Gst
except (ImportError, ValueError) as e:   # no gi/GStreamer on this host
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(f"pipeline needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")
    sys.exit(0)

pipeline_mod._new_buffer = lambda payload, pts, fid: ("buf", payload, pts, fid)

INTERVAL = 1_000_000_000 // 30


class _Source:
    encoded_caps = None
    encoded_parser = None
    active_timestamp_source = "system"
    ptp_locked = False
    tick_frequency_hz = 0
    reconnect_enabled = False

    def __init__(self, events):
        self.events = events

    def geometry(self):
        return (0, 0, 8, 8)

    def pixel_format(self):
        return "Mono8"

    def start(self, _on_frame, _on_encoded=None):
        self.events.append("source-start")

    def stop(self):
        self.events.append("source-stop")

    def close(self):
        self.events.append("source-close")

    def is_disconnected(self):
        return False


class _Appsrc:
    def __init__(self, events, name):
        self.events, self.name = events, name
        self.pushed = []

    def get_property(self, k):
        return 0

    def emit(self, sig, *args):
        if sig == "push-buffer":
            self.pushed.append(args[0])
            return Gst.FlowReturn.OK
        if sig == "end-of-stream":
            self.events.append(f"eos:{self.name}")
            return Gst.FlowReturn.OK
        raise AssertionError(sig)


class _FakeSession:
    def __init__(self, index, prefix, output_dir, description, *, header_factory, encoded, on_error, **_kw):
        self.index, self.prefix, self.output_dir, self.description = index, prefix, output_dir, description
        self.header_factory, self.encoded, self.on_error = header_factory, encoded, on_error
        self.path_base = os.path.join(output_dir, prefix)
        self.pipe = None                    # set by the factory
        self.events = None
        self.pushes = []
        self.start_calls = []
        self.finish_calls = []
        self.begin_calls = 0
        self.session_at_begin = "unset"
        self.result = PushResult.OK
        self.error = None
        self.fail_start = False

    def start(self, drops):
        self.start_calls.append(dict(drops))
        if self.fail_start:
            raise RuntimeError("no encoder")

    def push(self, payload, pts, stamp, caps_str=None):
        self.pushes.append((payload, pts, stamp.frame_id, caps_str))
        return self.result

    def begin_close(self):
        if self.begin_calls:      # idempotent, like the real one
            return
        self.begin_calls += 1
        self.session_at_begin = self.pipe._session
        self.events.append(f"begin:{self.index}")

    def finish_close(self, timeout, drops):
        self.finish_calls.append((timeout, dict(drops)))
        self.events.append(f"finish:{self.index}")
        return {"index": self.index, "prefix": self.prefix, "output_dir": self.output_dir,
                "started_unix_s": 0.0, "frames": len(self.pushes), "segments": 1, "truncated": False,
                "error": self.error, "skipped_awaiting_keyframe": 0, "csv": "", "json": "", "files": []}

    def describe(self, final=False):
        return {"index": self.index, "prefix": self.prefix, "frames": len(self.pushes), "segments": 0,
                "error": self.error, "skipped_awaiting_keyframe": 0, "output_dir": self.output_dir,
                "started_unix_s": 0.0}


class _MainBus:
    def add_signal_watch(self):
        pass

    def connect(self, _sig, _fn):
        pass


class _MainPipeline:
    def __init__(self, events):
        self.events = events

    def get_bus(self):
        return _MainBus()

    def set_state(self, st):
        self.events.append(f"main:{st.value_nick}")
        return Gst.StateChangeReturn.SUCCESS


def _pipe(tmp, stream_copy=False, enabled=True):
    events = []
    source = _Source(events)
    if stream_copy:
        source.encoded_caps, source.encoded_parser = "image/jpeg", "jpegparse"
    cfg = SimpleNamespace(
        camera=SimpleNamespace(frame_rate=30.0, reconnect_backoff_s=0.001, reconnect_backoff_max_s=0.002),
        recording=SimpleNamespace(enabled=enabled, output_dir=tmp, name_prefix="fake", bayer_pattern=None,
                                  segment_seconds=2),
        transport=SimpleNamespace(plugin_endpoint=SimpleNamespace(max_rate_hz=0.0)))
    p = CapturePipeline(cfg, source)
    p._recorder_desc = lambda rcfg, bits, loc, fps, color, parser: (
        f"queue ! fakesink name=rec_sink location={loc}", "stream-copy" if parser else "ffv1")
    p._session_desc_probe = "probe"
    p._raw_caps = "video/x-raw,format=GRAY8,width=8,height=8,framerate=30/1"
    p._image_size = 64
    p._fps = 30.0
    p._frame_interval_ns = INTERVAL
    p._stream_copy = stream_copy
    p._enc_parser = source.encoded_parser
    p.appsrc = _Appsrc(events, "camsrc")
    created = []

    def factory(*a, **k):
        s = _FakeSession(*a, **k)
        s.pipe, s.events = p, events
        created.append(s)
        return s

    p._session_factory = factory
    return p, created, events


def _stamp(fid, ts):
    return FrameStamp(frame_id=fid, timestamp_ns=ts, source=TimestampSource.SYSTEM,
                      system_ns=ts, camera_ns=ts, chunk_ns=None)


def test_activate_opens_a_session_with_a_fresh_prefix():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.drops.observe_frame(1)
        r = p.activate()
        assert r["ok"] and r["state"] == ACTIVE and p.get_state()["state"] == ACTIVE
        assert len(created) == 1 and p._session is created[0]
        s = created[0]
        assert s.prefix.startswith("fake-2") and s.output_dir == tmp and s.index == 1
        assert s.start_calls == [p.drops.summary()], "the drops snapshot the session's delta is measured from"
        assert "appsrc name=recsrc" in s.description and p._raw_caps in s.description
        assert f"location={tmp}/{s.prefix}" in s.description and s.encoded is False
        assert s.on_error == p._on_session_error


def test_activate_is_refused_when_already_recording_disabled_or_stopping():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        assert p.activate()["ok"]
        r = p.activate()
        assert not r["ok"] and r["error"] == "already recording" and len(created) == 1
        p, created, events = _pipe(tmp, enabled=False)
        r = p.activate()
        assert not r["ok"] and r["error"] == "recording disabled by config" and created == []
        p, created, events = _pipe(tmp)
        p._stopping = True
        assert not p.activate()["ok"] and created == []


def test_activate_start_failure_is_a_refusal_and_leaves_inactive():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        orig = p._session_factory

        def failing(*a, **k):
            s = orig(*a, **k)
            s.fail_start = True
            return s

        p._session_factory = failing
        r = p.activate()
        assert not r["ok"] and "no encoder" in r["error"]
        assert p._session is None and p.get_state()["state"] == INACTIVE


def test_deactivate_unpublishes_the_session_before_closing_it():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.activate()
        s = created[0]
        r = p.deactivate()
        assert r["ok"] and r["state"] == INACTIVE and p._session is None
        assert s.session_at_begin is None, "the feeders must stop seeing the session BEFORE its EOS"
        assert s.begin_calls == 1 and s.finish_calls[0][0] == SESSION_DRAIN_S
        assert p.get_state()["last_result"] is r
        r = p.deactivate()
        assert not r["ok"] and r["error"] == "not recording"


def test_two_sessions_get_distinct_prefixes():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.activate()
        open(os.path.join(tmp, created[0].prefix + ".csv"), "w").close()   # what the real session leaves
        p.deactivate()
        p.activate()
        assert created[0].prefix != created[1].prefix and created[1].index == 2


def test_raw_feed_goes_to_the_session_tiled_and_is_accounted():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p._on_frame(_stamp(1, 1_000), b"a" * 64)          # inactive: consumers only
        assert p.drops.frames == 1 and p.appsrc.pushed == [("buf", b"a" * 64, 0, 1)]
        p.activate()
        p._tiler = lambda b: b[::-1]
        p._on_frame(_stamp(2, 1_000 + INTERVAL), b"ab" * 32)
        s = created[0]
        assert s.pushes == [(b"ba" * 32, INTERVAL, 2, None)], "tiled bytes to the recorder, same PTS"
        assert p.appsrc.pushed[-1] == ("buf", b"ab" * 32, INTERVAL, 2), "the mosaic to the consumer tee"
        assert p.drops.frames == 2 and p.drops.enqueue_failures == 0


def test_a_dropped_recording_push_counts_a_skipped_one_does_not():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.activate()
        s = created[0]
        s.result = PushResult.DROPPED
        p._on_frame(_stamp(1, 1_000), b"a" * 64)
        assert p.drops.enqueue_failures == 1
        s.result = PushResult.SKIPPED
        p._on_frame(_stamp(2, 1_000 + INTERVAL), b"a" * 64)
        assert p.drops.enqueue_failures == 1 and p.drops.frames == 2


def test_stream_copy_decode_branch_never_feeds_or_accounts():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp, stream_copy=True)
        p.activate()
        p._on_frame(_stamp(1, 1_000), b"a" * 64)
        assert created[0].pushes == [] and p.drops.frames == 0 and p._base_ts is None
        assert p.appsrc.pushed == [("buf", b"a" * 64, 0, 1)], "consumers still get pixels"


def test_encoded_branch_owns_the_timeline_even_while_inactive():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp, stream_copy=True)
        p._on_encoded(_stamp(1, 1_000), b"j1", "image/jpeg")
        assert p._base_ts == 1_000 and p._last_pts == 0 and p.drops.frames == 1
        assert p._on_frame(_stamp(1, 1_000), b"a" * 64) is None
        assert p.appsrc.pushed[-1][2] == 0, "the decode branch reads the base the encoded branch set"
        p.activate()
        s = created[0]
        p._on_encoded(_stamp(2, 1_000 + INTERVAL), b"j2", "image/jpeg")
        assert s.pushes == [(b"j2", INTERVAL, 2, "image/jpeg")]
        s.result = PushResult.DROPPED
        p._on_encoded(_stamp(3, 1_000 + 2 * INTERVAL), b"j3", "image/jpeg")
        assert p.drops.enqueue_failures == 1 and p.drops.frames == 3


def test_forced_reencode_ignores_the_encoded_branch():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)          # _stream_copy False: _on_frame owns the timeline
        p._on_encoded(_stamp(1, 1_000), b"j1", "image/jpeg")
        assert p._base_ts is None and p.drops.frames == 0


def test_session_error_ends_the_session_not_the_process():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        ended = []
        p.on_session_ended = ended.append
        p.activate()
        s = created[0]
        s.error = "disk full"
        p._on_session_error(s)
        assert p._session is None and p.get_state()["state"] == INACTIVE
        assert s.finish_calls[0][0] == 0.0, "an errored pipeline may never EOS: don't wait for it"
        assert p._fatal is False and ended[0]["error"] == "disk full"
        p._on_session_error(s)                    # stale: already closed
        assert s.begin_calls == 1


def test_session_error_is_fatal_only_when_nothing_could_reactivate():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.session_error_fatal = True
        p.activate()
        created[0].error = "disk full"
        p._on_session_error(created[0])
        assert p._fatal is True and p.had_error


def test_request_stop_finalizes_the_session_first_then_the_main_pipeline():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.activate()
        del events[:]
        p.request_stop()
        assert events == ["source-stop", "begin:1", "eos:camsrc", "finish:1"], \
            "recorder EOS first (its drain overlaps the main one), main EOS, then the bounded wait"
        assert p._session is None and p._stopping and p.get_state()["state"] == INACTIVE
        assert created[0].finish_calls[0][0] == SESSION_DRAIN_S
        assert p.get_state()["last_result"]["ok"]


def test_shutdown_closes_a_session_the_error_path_left_open():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.pipeline = _MainPipeline(events)
        p.activate()
        del events[:]
        p.shutdown()                    # the main-bus ERROR path: loop quit without request_stop
        assert events[:4] == ["source-stop", "begin:1", "finish:1", "main:null"]
        assert "source-close" in events and p._session is None


def test_run_starts_the_boot_session_before_capture():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.pipeline = _MainPipeline(events)

        def on_playing():
            events.append("hook")
            assert p.activate()["ok"]
            GLib.idle_add(lambda: (p.loop.quit(), False)[1])
            return True

        p.run(on_playing=on_playing)
        assert events.index("main:playing") < events.index("hook") < events.index("source-start")
        assert created[0].start_calls, "the session is open before the first frame can arrive"
        assert "source-close" in events and p._session is None, "shutdown finalized it"


def test_run_aborts_when_the_boot_hook_fails():
    with tempfile.TemporaryDirectory() as tmp:
        p, created, events = _pipe(tmp)
        p.pipeline = _MainPipeline(events)
        p.run(on_playing=lambda: False)
        assert p.had_error and "source-start" not in events and "source-close" in events


def test_session_drain_fits_inside_the_core_stop_budget():
    # A cross-file invariant: the worst clean-stop chain is the larger of the force-quit timer and
    # (session EOS drain + sidecar join), then the reconnect join, then the device release -- and it
    # has to fit inside the supervisor's CORE_STOP_GRACE_S or kill() lands mid-finalization.
    from supervisor import CORE_STOP_GRACE_S
    worst = max(5.0, SESSION_DRAIN_S + 5.0) + _RECONNECT_JOIN_S
    assert worst < CORE_STOP_GRACE_S, (worst, CORE_STOP_GRACE_S)
    assert SESSION_DRAIN_S <= 5.0, "keep the session drain inside the process-level 5 s convention"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
