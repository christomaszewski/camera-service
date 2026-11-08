"""frame-capture service (docs/CAPTURE.md): settings validation, the request pipeline (select -> encode
-> EXIF -> write -> reply), the file and zenoh sinks, the zenoh adapter against fakes AND against a
real zenoh session (with a subscriber standing in for the vehicle store), the plugin's wiring from a
sensor config, and the supervisor launching it.

Run: python3 -m pytest core-driver/tests/test_capture_service.py
"""
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.capture import service as svc  # noqa: E402
from cam_driver.capture.__main__ import bayer_hint, build, plugin_params  # noqa: E402
from cam_driver.capture.encode import Encoded  # noqa: E402
from cam_driver.capture.frames import FrameRing  # noqa: E402
from cam_driver.capture.reader import Delivered  # noqa: E402
from cam_driver.capture.sinks import (FileSink, ZenohStoreSink, run_id_from_recording_dir,  # noqa: E402
                                      store_prefix)
from cam_driver.capture.timebase import TimeBase  # noqa: E402
from cam_driver.config import parse_config  # noqa: E402

MS = 1_000_000
_JPEG = b"\xFF\xD8" + b"\xFF\xDA\x00\x02" + b"img" + b"\xFF\xD9"


class _Encoder:
    quality = 77

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def encode(self, frame):
        self.calls += 1
        if self.fail:
            raise RuntimeError("encoder exploded")
        return Encoded(_JPEG, frame.width, frame.height)

    def close(self):
        pass


class _Sink:
    def __init__(self, name="mem", ok=True):
        self.name, self.ok, self.writes = name, ok, []

    def describe(self):
        return {"name": self.name}

    def write(self, base, jpeg, sidecar):
        self.writes.append((base, jpeg, json.loads(sidecar)))
        return {"ok": True, "where": base} if self.ok else {"ok": False, "error": "write_failed"}


def _service(sinks=None, encoder=None, settings=None, now=None):
    now = now or time.time_ns()
    s = svc.CaptureService(settings or svc.CaptureSettings(wait_ms=0), FrameRing(2.0), TimeBase(),
                           encoder or _Encoder(), sinks if sinks is not None else [_Sink()],
                           instance="cam_test", vehicle="7", run_id="run1")
    for i in range(10):
        stamp = now - (10 - i) * 40 * MS
        s.on_delivery(Delivered(100 + i, stamp, "system", "GRAY8", 8, 4, None, bytes(32), stamp + 2 * MS))
    return s, now


# ---- settings ------------------------------------------------------------------------------
def test_settings_defaults_and_validation():
    s = svc.CaptureSettings.from_params({"sinks": "zenoh", "tolerance_ms": 20})
    assert s.sinks == ["zenoh"] and s.tolerance_ms == 20 and s.buffer_s == 2.0
    for bad, match in [({"buffr_s": 1}, "unknown key"), ({"sinks": ["s3"]}, "sinks"), ({"sinks": []}, "sinks"),
                       ({"buffer_s": 0}, "buffer_s"), ({"wait_ms": -1}, "wait_ms"), ({"wait_ms": True}, "wait_ms"),
                       ({"queue": 0}, "queue"), ({"transport": "tcp"}, "transport"),
                       ({"bayer_pattern": "rgbg"}, "bayer_pattern"), ({"tolerance_ms": -1}, "tolerance_ms")]:
        with pytest.raises(ValueError, match=match):
            svc.CaptureSettings.from_params(bad)


# ---- the request pipeline --------------------------------------------------------------------
def test_a_timed_capture_with_gps_writes_jpeg_with_exif_and_a_sidecar():
    sink = _Sink()
    s, now = _service([sink])
    target = now - 4 * 40 * MS + 3 * MS
    reply = s.handle_payload(json.dumps({
        "stamp_ns": target, "tag": "overlap", "id": "r1", "meta": {"leg": 2},
        "gps": {"lat": 10.0, "lon": 20.0, "alt_m": 5.0, "alt_ref": "msl", "stamp_ns": target,
                "vel_enu": [0, 1, 0]},
        "orientation": {"yaw_deg": 45, "pitch_deg": 1}}).encode())
    assert reply["ok"] and reply["id"] == "r1" and reply["frame_id"] == 106 and reply["dt_ms"] == -3.0
    assert reply["gps"] == "extrapolated" and reply["basis"] == "capture" and reply["stamp_source"] == "system"
    base, jpeg, side = sink.writes[0]
    assert base == f"{reply['frame_stamp_ns']:019d}" and jpeg.startswith(b"\xFF\xD8\xFF\xE1")
    assert jpeg.endswith(_JPEG[2:])                       # the image itself is untouched
    assert side["run_id"] == "run1" and side["instance"] == "cam_test" and side["vehicle_id"] == "7"
    assert side["frame"]["frame_id"] == 106 and side["image"]["quality"] == 77
    assert side["request"]["meta"] == {"leg": 2} and side["orientation"] == {"yaw_deg": 45, "pitch_deg": 1}
    assert side["gps"]["method"] == "extrapolated" and side["gps"]["msl_alt_m"] == 5.0
    assert side["gps"]["applied"]["lat"] < 10.0           # moved 3 ms south (the frame is earlier)
    assert s.captures == 1 and s.last["frame_id"] == 106


def test_the_same_frame_twice_is_written_once():
    sink = _Sink()
    s, now = _service([sink])
    first = s.handle_payload(json.dumps({"stamp_ns": now - 2 * 40 * MS}).encode())
    again = s.handle_payload(json.dumps({"stamp_ns": now - 2 * 40 * MS + MS, "id": "x"}).encode())
    assert first["ok"] and again["ok"] and again["duplicate"] and again["id"] == "x"
    assert again["frame_id"] == first["frame_id"] and len(sink.writes) == 1


def test_refusals():
    s, now = _service()
    assert s.handle_payload(b"{bad")["error"] == "bad_request"
    old = s.handle_payload(json.dumps({"stamp_ns": now - 60 * 10**9}).encode())
    assert old["error"] == "too_old" and old["detail"]["frames"] == 10
    assert s.handle_payload(json.dumps({"stamp_ns": now + 10**9}).encode())["error"] == "no_frame_within_tolerance"
    assert s.failures == 2
    empty = svc.CaptureService(svc.CaptureSettings(wait_ms=0), FrameRing(), TimeBase(), _Encoder(), [_Sink()],
                               instance="c", vehicle="v", run_id=None)
    assert empty.handle_payload(None)["error"] == "no_frames"


def test_encode_and_write_failures():
    s, _ = _service(encoder=_Encoder(fail=True))
    r = s.handle_payload(None)
    assert not r["ok"] and r["error"] == "encode_failed" and "exploded" in r["detail"]
    s, _ = _service([_Sink("a", ok=False), _Sink("b", ok=False)])
    r = s.handle_payload(None)
    assert not r["ok"] and r["error"] == "write_failed" and set(r["sinks"]) == {"a", "b"}
    s, _ = _service([_Sink("a", ok=False), _Sink("b")])
    assert s.handle_payload(None)["ok"]                    # one sink is enough


def test_tolerance_defaults_to_one_frame_interval():
    s, _ = _service()
    assert s.default_tolerance_ns() == 40 * MS
    assert s.descriptor()["defaults"]["tolerance_ms"] == 40.0
    fixed, _ = _service(settings=svc.CaptureSettings(tolerance_ms=12, wait_ms=0))
    assert fixed.default_tolerance_ns() == 12 * MS
    fresh = svc.CaptureService(svc.CaptureSettings(), FrameRing(), TimeBase(), _Encoder(), [],
                               instance="c", vehicle="v", run_id=None)
    assert fresh.default_tolerance_ns() == 50 * MS


def test_descriptor_and_observers():
    events = []
    s, _ = _service()
    s.add_observer(events.append)
    s.add_observer(lambda r: 1 / 0)                       # a failing observer is contained
    s.handle_payload(None)
    d = s.descriptor()
    assert d["service"] == "frame-capture" and d["buffer"]["frames"] == 10 and d["captures"] == 1
    assert d["time"] == {"basis": "auto", "scale": "auto", "tai_offset_s": 37}
    assert d["sinks"] == [{"name": "mem"}] and len(events) == 1


# ---- sinks ---------------------------------------------------------------------------------
def test_file_sink_writes_both_files_atomically():
    with tempfile.TemporaryDirectory() as d:
        sink = FileSink(os.path.join(d, "rec", "keyframes"))
        r = sink.write("0001", b"jpeg", b"{}")
        assert r["ok"] and open(r["jpg"], "rb").read() == b"jpeg" and open(r["json"], "rb").read() == b"{}"
        assert sorted(os.listdir(sink.directory)) == ["0001.jpg", "0001.json"]       # no temp files left
        blocked = FileSink(os.path.join(r["jpg"], "sub"))                            # a FILE in the way
        assert blocked.write("x", b"", b"")["error"] == "write_failed"


def test_run_id_and_store_prefix():
    with tempfile.TemporaryDirectory() as d:
        rec = os.path.join(d, "runs", "20261108T1400Z_a", "recordings", "cam")
        assert run_id_from_recording_dir(rec, d) == "20261108T1400Z_a"
        assert run_id_from_recording_dir(os.path.join(d, "recordings", "cam"), d) is None
        assert run_id_from_recording_dir(rec, "") is None
    assert store_prefix(svc.CaptureSettings().store_prefix, "7", "run1", "cam") == "fleet/7/store/run1/cam/keyframes"
    assert store_prefix("fleet/{vehicle}/store/{run}/{instance}", "7", None, "cam") is None
    assert store_prefix("fleet/{vehicle}/kf/{instance}", "7", None, "cam") == "fleet/7/kf/cam"
    with pytest.raises(ValueError, match="concrete"):
        store_prefix("fleet/*/store/{instance}", "7", "r", "cam")


class _Matching:
    def __init__(self, v):
        self.matching = v


class _Probe:
    def __init__(self, matched):
        self.matched = matched
        self.undeclared = False

    @property
    def matching_status(self):
        return _Matching(self.matched)

    def undeclare(self):
        self.undeclared = True


class _StoreSession:
    def __init__(self, matched=True, fail=False):
        self.matched, self.fail, self.puts = matched, fail, []

    def declare_publisher(self, key):
        self.probe = _Probe(self.matched)
        self.probe_key = key
        return self.probe

    def put(self, key, payload, **kw):
        if self.fail:
            raise RuntimeError("router gone")
        self.puts.append((key, payload, kw))


def test_zenoh_sink_states_and_puts():
    assert ZenohStoreSink(None).write("b", b"j", b"m")["error"] == "no_run"
    sink = ZenohStoreSink("fleet/7/store/run1/cam/keyframes")
    assert sink.describe()["store"] == "no_session" and sink.write("b", b"j", b"m")["error"] == "no_session"
    sess = _StoreSession(matched=False)
    sink.attach(sess, None)
    assert sess.probe_key == "fleet/7/store/run1/cam/keyframes"
    assert sink.write("b", b"j", b"m")["error"] == "store_absent" and not sess.puts
    sess.probe.matched = True
    r = sink.write("b", b"j", b"m")
    assert r == {"ok": True, "jpg": "fleet/7/store/run1/cam/keyframes/b.jpg",
                 "json": "fleet/7/store/run1/cam/keyframes/b.json"}
    assert [p[0] for p in sess.puts] == [r["jpg"], r["json"]]           # the image first, then the sidecar
    sess.fail = True
    assert sink.write("c", b"j", b"m")["error"] == "put_failed"
    sink.detach()
    assert sess.probe.undeclared and sink.describe()["store"] == "no_session"


# ---- zenoh adapter (fakes) -----------------------------------------------------------------
class _Decl:
    def __init__(self, key, cb=None):
        self.key, self.cb, self.undeclared, self.puts = key, cb, False, []

    def undeclare(self):
        self.undeclared = True

    def put(self, payload, **kw):
        self.puts.append(json.loads(payload))


class _Liveliness:
    def __init__(self, session):
        self.session = session

    def declare_token(self, key):
        self.session.token = _Decl(key)
        return self.session.token


class _Session:
    def __init__(self, endpoints):
        self.endpoints, self.queryables, self.publishers, self.token, self.closed = endpoints, {}, {}, None, False

    def declare_queryable(self, key, cb):
        self.queryables[key] = _Decl(key, cb)
        return self.queryables[key]

    def declare_publisher(self, key):
        self.publishers[key] = _Decl(key)
        return self.publishers[key]

    def liveliness(self):
        return _Liveliness(self)

    def close(self):
        self.closed = True


class _Query:
    def __init__(self, payload=None, parameters=""):
        self.payload, self.parameters, self.replies, self.done = payload, parameters, [], threading.Event()

    def reply(self, key, payload, encoding=None):
        self.replies.append((key, json.loads(payload)))
        self.done.set()


def test_adapter_declares_serves_and_publishes():
    s, _ = _service()
    sessions = []
    z = svc.ZenohCapture(s, svc.capture_key("7", "cam_test"), ["tcp/x:7447"],
                         session_factory=lambda eps: sessions.append(_Session(eps)) or sessions[-1])
    z.start()
    try:
        sess = sessions[0]
        assert z.active and sess.endpoints == ["tcp/x:7447"] and sess.token.key == "fleet/7/svc/cam_test/capture"
        assert set(sess.queryables) == {"fleet/7/svc/cam_test/capture", "fleet/7/svc/cam_test/capture/request"}
        q = _Query()
        sess.queryables["fleet/7/svc/cam_test/capture"].cb(q)
        assert q.replies[0][0] == "fleet/7/svc/cam_test/capture" and q.replies[0][1]["instance"] == "cam_test"
        q = _Query(payload=json.dumps({"tag": "manual"}).encode())
        sess.queryables["fleet/7/svc/cam_test/capture/request"].cb(q)
        assert q.done.wait(5) and q.replies[0][0] == "fleet/7/svc/cam_test/capture/request"
        assert q.replies[0][1]["ok"]
        events = sess.publishers["fleet/7/svc/cam_test/capture/events"].puts
        assert events and events[0]["instance"] == "cam_test" and events[0]["frame_id"] == 109
    finally:
        z.close()
    assert sessions[0].closed and sessions[0].token.undeclared


def test_a_full_queue_answers_busy_at_once():
    s, _ = _service()
    z = svc.ZenohCapture(s, "k", [], queue_size=1, session_factory=lambda eps: _Session(eps))
    z._jobs.put_nowait("occupied")                          # no worker running: the queue stays full
    q = _Query()
    z._on_request(q)
    assert q.replies[0][1]["error"] == "busy"


def test_open_failure_is_contained_and_retried():
    s, _ = _service()
    attempts = []

    def factory(eps):
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("no router")
        return _Session(eps)

    z = svc.ZenohCapture(s, "k", [], session_factory=factory, retry_s=0.05)
    z.start()
    try:
        deadline = time.monotonic() + 5
        while not z.active and time.monotonic() < deadline:
            time.sleep(0.02)
        assert z.active and len(attempts) == 2
    finally:
        z.close()


# ---- a real zenoh session ------------------------------------------------------------------
def _free_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{sock.getsockname()[1]}"


def _peer(zenoh, endpoint, listen):
    cfg = zenoh.Config()
    cfg.insert_json5("mode", '"peer"')
    cfg.insert_json5("scouting/multicast/enabled", "false")
    # Explicit IPv4 listeners on BOTH sides: the default peer listener is tcp/[::]:0, which fails
    # on hosts without IPv6.
    cfg.insert_json5("listen/endpoints", json.dumps([endpoint if listen else "tcp/127.0.0.1:0"]))
    if not listen:
        cfg.insert_json5("connect/endpoints", json.dumps([endpoint]))
    return zenoh.open(cfg)


def test_round_trip_over_real_zenoh_into_a_store():
    import zenoh   # in every CI image; this test must not skip
    endpoint = _free_endpoint()
    client = _peer(zenoh, endpoint, listen=True)
    stored = queue.Queue()
    store_sink = ZenohStoreSink("fleet/7/store/run1/cam_test/keyframes")
    s, _ = _service([store_sink])

    def open_session(eps):
        z._zenoh = zenoh               # what the default factory records, for encodings + the store sink
        return _peer(zenoh, eps[0], listen=False)

    z = svc.ZenohCapture(s, svc.capture_key("7", "cam_test"), [endpoint], store_sinks=[store_sink],
                         session_factory=open_session)
    z.start()
    storage = None
    try:
        key = "fleet/7/svc/cam_test/capture/request"

        def ask(obj):
            replies = list(client.get(key, payload=json.dumps(obj).encode(), timeout=10))
            return json.loads(replies[0].ok.payload.to_bytes())

        deadline = time.monotonic() + 10
        reply = None
        while time.monotonic() < deadline:
            replies = list(client.get(key, payload=b"{}", timeout=2))
            if replies and replies[0].ok:
                reply = json.loads(replies[0].ok.payload.to_bytes())
                break
            time.sleep(0.1)
        assert reply is not None and not reply["ok"] and reply["error"] == "store_absent"
        # A subscriber on the run's keyspace is what a Zenoh storage is: now the puts land.
        storage = client.declare_subscriber("fleet/7/store/run1/**",
                                            lambda smp: stored.put((str(smp.key_expr), smp.payload.to_bytes())))
        deadline = time.monotonic() + 10
        while store_sink.store_state() != "present" and time.monotonic() < deadline:
            time.sleep(0.05)
        reply = ask({"id": "real"})
        assert reply["ok"] and reply["id"] == "real" and reply["sinks"]["zenoh"]["ok"]
        k1, jpeg = stored.get(timeout=5)
        k2, side = stored.get(timeout=5)
        assert k1.endswith(".jpg") and jpeg.startswith(b"\xFF\xD8\xFF\xE1") and k2.endswith(".json")
        assert json.loads(side)["run_id"] == "run1"
    finally:
        if storage is not None:
            storage.undeclare()
        z.close()
        client.close()


# ---- wiring from a sensor config, and the supervisor -----------------------------------------
_CFG = """
camera: {type: gige, frame_rate: 20}
gige: {fake: true, pixel_format: BayerRG8}
recording: {enabled: false}
transport: {plugin_endpoint: {enabled: true, socket_path: %s}}
plugins:
  - name: frame-capture
    isolation: process
    params: {sinks: [file, zenoh], buffer_s: 1.5, quality: 70}
"""


def test_build_wires_everything_from_the_sensor_config(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "data", "runs", "R1"))
        os.symlink("runs/R1", os.path.join(d, "data", "current"))
        cfg = os.path.join(d, "cam.yaml")
        with open(cfg, "w") as f:
            f.write(_CFG % os.path.join(d, "sock", "frames"))
        monkeypatch.setenv("RIG_DATA_DIR", os.path.join(d, "data"))
        monkeypatch.setenv("CAM_INSTANCE", "cam_a")
        monkeypatch.setenv("VEHICLE_ID", "9")
        service, reader, control = build(cfg, "frame-capture")
        assert service.run_id == "R1" and service.instance == "cam_a" and service.vehicle == "9"
        file_sink, zenoh_sink = service.sinks
        assert file_sink.directory == os.path.join(os.path.realpath(os.path.join(d, "data")), "runs", "R1",
                                                   "recordings", "cam_a", "keyframes")
        assert zenoh_sink.prefix == "fleet/9/store/R1/cam_a/keyframes"
        assert reader.bayer_hint == "rggb" and reader.plugin_socket.endswith("sock/frames")
        assert control.key_base == "fleet/9/svc/cam_a/capture" and service.ring.span_ns == 1_500_000_000
        assert service.encoder.quality == 70
        service.encoder.close()


def test_bayer_hint_and_plugin_lookup():
    cfg = parse_config({"camera": {"type": "gige"}, "gige": {"pixel_format": "BayerBG8"}})
    assert bayer_hint(cfg, "auto") == "bggr" and bayer_hint(cfg, "none") is None
    assert bayer_hint(cfg, "grbg") == "grbg"
    rec = parse_config({"camera": {"type": "usb"}, "recording": {"bayer_pattern": "GBRG"}})
    assert bayer_hint(rec, "auto") == "gbrg"
    assert bayer_hint(parse_config({"camera": {"type": "rtsp"}}), "auto") is None
    with pytest.raises(ValueError, match="no enabled"):
        plugin_params(cfg, "frame-capture")
    off = parse_config({"plugins": [{"name": "frame-capture", "enabled": False}]})
    with pytest.raises(ValueError, match="no enabled"):
        plugin_params(off, "frame-capture")


def test_supervisor_launches_the_plugin_with_the_sensor_config(tmp_path):
    from supervisor import Supervisor
    cfg = tmp_path / "cam.yaml"
    cfg.write_text(_CFG % (tmp_path / "frames"))
    sup = Supervisor(str(cfg))
    sup._build_services()
    plugin = [s for s in sup.services if s.name == "frame-capture"]
    assert len(plugin) == 1 and not plugin[0].critical and plugin[0].restart
    assert plugin[0].cmd[1:] == ["-m", "cam_driver.capture", "-c", str(cfg), "--plugin", "frame-capture"]
