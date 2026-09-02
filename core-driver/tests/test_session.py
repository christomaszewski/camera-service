"""Tests for RecordingSession (cam_driver.session): one recording session's pipeline lifecycle, push
accounting, the stream-copy keyframe gate, and the close ordering that keeps a finalized .mkv + a
self-attesting sidecar -- against fake pipeline / bus / appsrc / sidecar objects.

session.py imports gi at module scope, so this SKIPs on a bare host (the dev container and CI have the
bindings). No GStreamer objects are built: the buffer and caps constructors are monkeypatched, and
the "next loop iteration" scheduling runs inline.

Run: python3 core-driver/tests/test_session.py
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import cam_driver.session as session_mod
    from cam_driver.session import PushResult, RecordingSession
    from gi.repository import Gst
except (ImportError, ValueError) as e:   # no gi/GStreamer on this host
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(f"session needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")
    sys.exit(0)

session_mod._wrap_buffer = lambda payload, pts, fid: ("buf", payload, pts, fid)
session_mod._caps_from_string = lambda s: ("caps", s)
session_mod.GLib = SimpleNamespace(idle_add=lambda fn, *a: fn(*a))   # "next iteration" -> inline

H264_BS = "video/x-h264, stream-format=(string)byte-stream, alignment=(string)au"
IDR = b"\x00\x00\x00\x01\x65\x88\x84"
P_FRAME = b"\x00\x00\x00\x01\x41\x9a\x00"


class _Appsrc:
    def __init__(self, order, max_bytes=0, level=0, flow=None):
        self.order = order
        self.props = {"max-bytes": max_bytes, "current-level-bytes": level}
        self.pushed = []
        self.caps = []
        self.eos = 0
        self.flow = Gst.FlowReturn.OK if flow is None else flow

    def get_property(self, k):
        return self.props[k]

    def set_property(self, k, v):
        if k == "caps":
            self.caps.append(v)
        else:
            self.props[k] = v

    def emit(self, sig, *args):
        if sig == "push-buffer":
            self.pushed.append(args[0])
            self.order.append("push")
            return self.flow
        if sig == "end-of-stream":
            self.eos += 1
            self.order.append("eos")
            return Gst.FlowReturn.OK
        raise AssertionError(sig)


class _Msg:
    def __init__(self, type_, structure=None):
        self.type = type_
        self._s = structure

    def parse_error(self):
        return "boom", "debug"

    def get_structure(self):
        return self._s


class _Structure:
    def __init__(self, name, location=None):
        self._name, self._loc = name, location

    def get_name(self):
        return self._name

    def get_string(self, _k):
        return self._loc


class _Bus:
    def __init__(self, order, pop_msg="eos"):
        self.order = order
        self.handler = None
        self.watch = self.removed = 0
        self.pop_calls = []
        self.pop_msg = {"eos": _Msg(Gst.MessageType.EOS), "error": _Msg(Gst.MessageType.ERROR),
                        None: None}[pop_msg]

    def add_signal_watch(self):
        self.watch += 1
        self.order.append("watch")

    def connect(self, _sig, fn):
        self.handler = fn

    def remove_signal_watch(self):
        self.removed += 1
        self.order.append("remove")

    def timed_pop_filtered(self, timeout, types):
        self.pop_calls.append((timeout, types))
        self.order.append("pop")
        return self.pop_msg


class _Pipeline:
    def __init__(self, appsrc, bus, order, fail_state=False):
        self.appsrc, self.bus, self.order, self.fail = appsrc, bus, order, fail_state
        self.states = []

    def get_by_name(self, name):
        return self.appsrc if name == "recsrc" else None

    def get_bus(self):
        return self.bus

    def set_state(self, st):
        self.states.append(st)
        self.order.append(f"state:{st.value_nick}")
        return Gst.StateChangeReturn.FAILURE if self.fail else Gst.StateChangeReturn.SUCCESS


class _Sidecar:
    def __init__(self, base, order):
        self.base, self.order = base, order
        self.started = self.stopped = 0
        self.header = None
        self.headers = 0
        self.rows = []
        self.summary = None
        self.extra = None

    @property
    def csv_path(self):
        return self.base + ".csv"

    @property
    def json_path(self):
        return self.base + ".json"

    def start(self):
        self.started += 1
        self.order.append("sidecar-start")

    def stop(self):
        self.stopped += 1
        self.order.append("sidecar-stop")

    def write_header(self, h):
        self.header, self.headers = h, self.headers + 1

    def add(self, stamp, pts):
        self.rows.append((stamp.frame_id, pts))

    def write_summary(self, summary, extra=None):
        self.summary, self.extra = summary, extra
        self.order.append("summary")


def _stamp(fid=1, ts=1_000):
    return SimpleNamespace(frame_id=fid, timestamp_ns=ts)


def _header(stamp, pts, sess):
    return {"base": 5, "first_pts": pts, "first_fid": stamp.frame_id, "index": sess.index}


def _session(tmp, *, encoded=False, pop_msg="eos", flow=None, fail_state=False, on_error=None,
             max_bytes=0, level=0):
    order = []
    appsrc = _Appsrc(order, max_bytes, level, flow)
    bus = _Bus(order, pop_msg)
    pipe = _Pipeline(appsrc, bus, order, fail_state)
    holder = {}

    def sidecar_factory(base):
        holder["sc"] = _Sidecar(base, order)
        return holder["sc"]

    s = RecordingSession(1, "cam-x", tmp, "appsrc name=recsrc ! fakesink", header_factory=_header,
                         encoded=encoded, on_error=on_error, parse_launch=lambda _d: pipe,
                         sidecar_factory=sidecar_factory)
    return s, appsrc, bus, pipe, holder["sc"], order


DROPS0 = {"frames": 10, "source_gaps": 0, "frames_missing": 0, "enqueue_failures": 0,
          "publish_drops": 1, "pts_rebases": 0}
DROPS1 = {"frames": 25, "source_gaps": 1, "frames_missing": 2, "enqueue_failures": 1,
          "publish_drops": 1, "pts_rebases": 0}


def test_start_opens_the_sidecar_before_playing_and_watches_the_bus():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        assert order == ["sidecar-start", "watch", "state:playing"]
        assert bus.handler is not None and s.started_unix_s is not None
        assert s.drops_at_start == DROPS0 and sc.base == os.path.join(tmp, "cam-x")


def test_start_failure_leaves_nothing_half_open():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, fail_state=True)
        try:
            s.start(DROPS0)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a pipeline that refuses PLAYING must raise")
        assert pipe.states[-1] == Gst.State.NULL and sc.stopped == 1


def test_first_push_writes_the_header_once_and_every_push_a_row():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        assert s.push(b"a" * 8, 100, _stamp(1)) is PushResult.OK
        assert s.push(b"b" * 8, 133, _stamp(2)) is PushResult.OK
        assert sc.headers == 1 and sc.header == {"base": 5, "first_pts": 100, "first_fid": 1, "index": 1}
        assert s.first_pts == 100 and s.frames == 2
        assert sc.rows == [(1, 100), (2, 133)]
        assert appsrc.pushed == [("buf", b"a" * 8, 100, 1), ("buf", b"b" * 8, 133, 2)]


def test_a_full_feed_is_a_drop_with_no_row_and_no_header():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, max_bytes=16, level=12)
        s.start(DROPS0)
        assert s.push(b"a" * 8, 100, _stamp(1)) is PushResult.DROPPED
        assert sc.headers == 0 and sc.rows == [] and s.frames == 0 and appsrc.pushed == []


def test_a_rejected_push_is_a_drop_with_no_row():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, flow=Gst.FlowReturn.FLUSHING)
        s.start(DROPS0)
        assert s.push(b"a" * 8, 100, _stamp(1)) is PushResult.DROPPED
        assert sc.rows == [] and s.frames == 0


def test_h264_session_waits_for_a_keyframe_then_applies_caps_once():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, encoded=True)
        s.start(DROPS0)
        assert s.push(P_FRAME, 100, _stamp(1), H264_BS) is PushResult.SKIPPED
        assert s.push(P_FRAME, 133, _stamp(2), H264_BS) is PushResult.SKIPPED
        assert s.skipped_awaiting_keyframe == 2 and sc.headers == 0 and appsrc.caps == []
        assert s.push(IDR, 166, _stamp(3), H264_BS) is PushResult.OK
        assert s.push(P_FRAME, 200, _stamp(4), H264_BS) is PushResult.OK, "after the IDR, every frame"
        assert appsrc.caps == [("caps", H264_BS)], "negotiated caps applied exactly once, on the first push"
        assert sc.header["first_pts"] == 166 and sc.rows == [(3, 166), (4, 200)]


def test_mjpeg_session_records_from_the_first_frame():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, encoded=True)
        s.start(DROPS0)
        assert s.push(b"\xff\xd8\xff", 100, _stamp(1), "image/jpeg, width=(int)8") is PushResult.OK
        assert s.skipped_awaiting_keyframe == 0 and appsrc.caps == [("caps", "image/jpeg, width=(int)8")]


def test_begin_close_stops_the_feed_and_sends_eos_once():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        s.push(b"a" * 8, 100, _stamp(1))
        s.begin_close()
        s.begin_close()
        assert s.closed and appsrc.eos == 1
        assert s.push(b"b" * 8, 133, _stamp(2)) is PushResult.SKIPPED, "a frame racing the EOS is not a loss"
        assert s.frames == 1 and len(sc.rows) == 1


def test_finish_close_order_and_attestation():
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("cam-x-00000.mkv", "cam-x-00001.mkv", "cam-y-00000.mkv"):
            open(os.path.join(tmp, name), "w").close()
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        s.push(b"a" * 8, 100, _stamp(1))
        s.begin_close()
        del order[:]
        info = s.finish_close(5.0, DROPS1)
        assert order == ["remove", "pop", "state:null", "sidecar-stop", "summary"], \
            "watch off BEFORE the EOS pop; NULL; sidecar joined BEFORE the JSON summary"
        assert bus.pop_calls[0][0] == 5 * Gst.SECOND
        assert info["truncated"] is False and info["error"] is None and info["frames"] == 1
        assert info["files"] == [os.path.join(tmp, "cam-x-00000.mkv"), os.path.join(tmp, "cam-x-00001.mkv")]
        assert info["csv"].endswith("cam-x.csv") and info["json"].endswith("cam-x.json")
        assert sc.summary == {"frames": 15, "source_gaps": 1, "frames_missing": 2, "enqueue_failures": 1,
                              "publish_drops": 0, "pts_rebases": 0}, "the session's DELTA of the process counters"
        assert sc.extra["session"]["frames_recorded"] == 1 and sc.extra["session"]["segments"] == 2
        assert sc.extra["session"]["first_pts_ns"] == 100 and sc.extra["session"]["truncated"] is False


def test_finish_close_timeout_is_attested_as_truncated_and_still_nulls():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, pop_msg=None)
        s.start(DROPS0)
        s.begin_close()
        info = s.finish_close(0.01, DROPS1)
        assert info["truncated"] is True and info["error"] is None
        assert pipe.states[-1] == Gst.State.NULL, "NULL on every path: it is what releases the encoder"
        assert sc.extra["session"]["truncated"] is True


def test_finish_close_error_during_drain_is_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp, pop_msg="error")
        s.start(DROPS0)
        s.begin_close()
        info = s.finish_close(1.0, DROPS1)
        assert info["truncated"] is True and "boom" in info["error"]


def test_no_eos_wait_when_the_session_already_errored_or_saw_eos():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        s.error = "disk full"
        s.begin_close()
        info = s.finish_close(5.0, DROPS1)
        assert bus.pop_calls == [] and info["truncated"] is True and info["error"] == "disk full"

        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        bus.handler(bus, _Msg(Gst.MessageType.EOS))   # the watch already dispatched the EOS
        s.begin_close()
        info = s.finish_close(5.0, DROPS1)
        assert bus.pop_calls == [] and info["truncated"] is False


def test_bus_error_reports_once_on_the_next_iteration():
    with tempfile.TemporaryDirectory() as tmp:
        reported = []
        s, appsrc, bus, pipe, sc, order = _session(tmp, on_error=reported.append)
        s.start(DROPS0)
        bus.handler(bus, _Msg(Gst.MessageType.ERROR))
        bus.handler(bus, _Msg(Gst.MessageType.ERROR))
        assert reported == [s] and s.error == "boom | debug"


def test_fragment_closed_messages_collect_the_segment_list():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        bus.handler(bus, _Msg(Gst.MessageType.ELEMENT, _Structure("splitmuxsink-fragment-closed", "/r/cam-x-00000.mkv")))
        bus.handler(bus, _Msg(Gst.MessageType.ELEMENT, _Structure("splitmuxsink-fragment-opened", "/r/cam-x-00001.mkv")))
        bus.handler(bus, _Msg(Gst.MessageType.ELEMENT, None))
        assert s.segments == ["/r/cam-x-00000.mkv"]
        s.begin_close()
        info = s.finish_close(1.0, DROPS1)
        assert info["files"] == ["/r/cam-x-00000.mkv"], "reported fragments win over the glob"


def test_zero_frame_session_still_attests_itself():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        s.begin_close()
        info = s.finish_close(1.0, DROPS0)
        assert sc.headers == 0 and sc.extra["session"]["frames_recorded"] == 0 and info["frames"] == 0
        assert sc.summary["frames"] == 0


def test_describe_is_plain_scalars():
    with tempfile.TemporaryDirectory() as tmp:
        s, appsrc, bus, pipe, sc, order = _session(tmp)
        s.start(DROPS0)
        s.push(b"a" * 8, 100, _stamp(1))
        d = s.describe()
        assert d["index"] == 1 and d["prefix"] == "cam-x" and d["frames"] == 1 and d["error"] is None
        assert all(isinstance(v, (int, float, str, type(None))) for v in d.values())


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
