"""Tests for service health (cam_driver.health + cam_driver.health_genicam): the ROS-diagnostics
snapshot contract (docs/HEALTH.md), limits, merging, staleness, the recording-session summary, the
pipeline provider's stream/recording rules, the GenICam provider against a stub device, the
from_config wiring and the file output.

Pure Python -- no gi, no zenoh, no Aravis -- so this runs (and must not skip) in CI.

Run: python3 core-driver/tests/test_health.py
"""
import json
import logging
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.config import health_file, parse_config  # noqa: E402
from cam_driver.health import (ERROR, OK, STALE, WARN, HealthMonitor, HealthWindow,  # noqa: E402
                               PipelineProvider, Report, build_snapshot, evaluate_limits, health_key,
                               merge, resolve_limits)
from cam_driver.health_genicam import GenicamProvider, parse_value, snake  # noqa: E402


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _assert_ros_mappable(snap):
    """The contract: every entry maps losslessly onto diagnostic_msgs/DiagnosticStatus."""
    assert set(snap) == {"schema_version", "service", "instance", "stamp_unix_ns", "level", "status"}
    assert isinstance(snap["stamp_unix_ns"], int)
    assert snap["level"] == max((s["level"] for s in snap["status"]), default=OK)
    for st in snap["status"]:
        assert set(st) == {"level", "name", "message", "hardware_id", "values"}
        assert st["level"] in (OK, WARN, ERROR, STALE)
        assert all(isinstance(st[k], str) for k in ("name", "message", "hardware_id"))
        assert st["name"].startswith(snap["instance"] + ": ")
        for k, v in st["values"].items():
            assert isinstance(k, str)
            assert v is None or isinstance(v, (bool, int, float, str)), (k, v)
    json.loads(json.dumps(snap, allow_nan=False))   # strict JSON: a browser must parse it


# ---- limits + merge -------------------------------------------------------------
def test_limits_pick_the_worst_crossed_bound_and_ignore_non_numbers():
    lim = {"temp.sensor_c": {"warn_above": 65, "error_above": 75},
           "disk.free_pct": {"warn_below": 10, "error_below": 2}}
    assert evaluate_limits({"temp.sensor_c": 60.0}, lim) == (OK, [])
    assert evaluate_limits({"temp.sensor_c": 70.0}, lim) == (WARN, ["temp.sensor_c 70 > 65"])
    assert evaluate_limits({"temp.sensor_c": 80.5}, lim) == (ERROR, ["temp.sensor_c 80.5 > 75"])
    assert evaluate_limits({"disk.free_pct": 1.0}, lim) == (ERROR, ["disk.free_pct 1 < 2"])
    assert evaluate_limits({"temp.sensor_c": True}, lim) == (OK, []), "a bool is not a temperature"
    assert evaluate_limits({"temp.sensor_c": "hot"}, lim) == (OK, [])
    assert evaluate_limits({"temp.sensor_c": None}, lim) == (OK, [])


def test_resolve_limits_ships_only_the_disk_default_and_lets_config_replace_it():
    assert resolve_limits(None) == {"disk.free_pct": {"warn_below": 10.0, "error_below": 2.0}}
    r = resolve_limits({"disk.free_pct": {"warn_below": 20}, "temp.sensor_c": {"warn_above": 60}})
    assert r["disk.free_pct"] == {"warn_below": 20.0}, "a configured key replaces the whole rule"
    assert r["temp.sensor_c"] == {"warn_above": 60.0}


def test_merge_groups_by_component_names_by_instance_and_keeps_the_first_hardware_id():
    out = merge([Report("stream", OK, "25 fps", values={"frames": 10}),
                 Report("camera", OK, "41 C", hardware_id="FLIR Boson sn 1", values={"temp.sensor_c": 41.0}),
                 Report("stream", OK, values={"aravis.n_failures": 0})], "cam_a", {})
    assert [s["name"] for s in out] == ["cam_a: stream", "cam_a: camera"], "first-seen order"
    assert out[0]["values"] == {"frames": 10, "aravis.n_failures": 0}
    assert out[0]["message"] == "25 fps" and out[0]["hardware_id"] == ""
    assert out[1]["hardware_id"] == "FLIR Boson sn 1"


def test_merge_levels_messages_and_limits():
    lim = {"temp.sensor_c": {"warn_above": 65}}
    [s] = merge([Report("camera", OK, "70 C", values={"temp.sensor_c": 70.0})], "c", lim)
    assert s["level"] == WARN and s["message"] == "temp.sensor_c 70 > 65", "a limit raises the level + says why"
    [s] = merge([Report("camera", ERROR, "PTP gone", values={"temp.sensor_c": 70.0})], "c", lim)
    assert s["level"] == ERROR and s["message"] == "PTP gone", "a worse provider level wins"
    [s] = merge([Report("camera", WARN, "PTP not locked", values={"temp.sensor_c": 70.0})], "c", lim)
    assert s["level"] == WARN and s["message"] == "PTP not locked; temp.sensor_c 70 > 65"
    [s] = merge([Report("camera", OK, values={})], "c", {})
    assert s["message"] == "OK"


def test_values_are_forced_to_json_safe_scalars():
    [s] = merge([Report("x", OK, values={"nan": float("nan"), "inf": float("inf"), "obj": [1, 2],
                                         "ok": 1.5, "b": False})], "c", {})
    assert s["values"] == {"nan": None, "inf": None, "obj": "[1, 2]", "ok": 1.5, "b": False}
    _assert_ros_mappable(build_snapshot("c", 1, [s]))


def test_key_names():
    assert health_key("veh", "cam_a") == "fleet/veh/svc/cam_a/health"


# ---- the session window --------------------------------------------------------------
def _snap(ns, level, temp, msg="m"):
    return build_snapshot("c", ns, [{"level": level, "name": "c: camera", "message": msg,
                                     "hardware_id": "hw", "values": {"temp.sensor_c": temp, "ptp.state": "Slave"}}])


def test_window_tracks_extremes_and_the_worst_level():
    w = HealthWindow()
    for ns, lvl, t, m in ((1, OK, 40.0, "a"), (2, WARN, 70.0, "hot"), (3, OK, 50.0, "b")):
        w.add(_snap(ns, lvl, t, m))
    s = w.summary()
    assert s["samples"] == 3 and s["from_unix_ns"] == 1 and s["to_unix_ns"] == 3
    cam = s["status"]["c: camera"]
    assert cam["worst_level"] == WARN and cam["worst_message"] == "hot" and cam["hardware_id"] == "hw"
    assert cam["values"]["temp.sensor_c"] == {"first": 40.0, "last": 50.0, "min": 40.0, "max": 70.0}
    assert cam["values"]["ptp.state"] == "Slave", "strings keep their last value"
    json.dumps(s, allow_nan=False)


# ---- the pipeline provider -------------------------------------------------------------
class _Pipe:
    def __init__(self):
        self.frames = 0
        self.gaps = self.missing = self.enq = 0
        self.fps = 25.0
        self.reconnecting = False
        self.state = "inactive"
        self.recording_enabled = True
        self.recording = None
        self.last_error = None

    def descriptor(self):
        return {"state": self.state, "recording_enabled": self.recording_enabled,
                "recording": self.recording, "last_error": self.last_error,
                "health": {"frames": self.frames, "source_gaps": self.gaps, "frames_missing": self.missing,
                           "enqueue_failures": self.enq, "publish_drops": 0, "pts_rebases": 0,
                           "stalled": False, "reconnecting": self.reconnecting}}


def _provider(p, clock, out_dir="", playback=None):
    return PipelineProvider(p.descriptor, frame_rate=lambda: p.fps, reconnecting=lambda: p.reconnecting,
                            playback=playback, timestamp_source=lambda: "ptp_chunk", output_dir=out_dir,
                            stall_after_s=5.0, loss_hold_s=10.0, clock=clock)


def _stream(prov):
    return next(r for r in prov.poll() if r.component == "stream")


def test_stream_measures_fps_and_is_ok_while_frames_flow():
    p, clock = _Pipe(), Clock()
    prov = _provider(p, clock)
    r = _stream(prov)
    assert r.level == OK and r.message == "starting" and "fps.delivered" not in r.values
    p.frames += 25
    clock.t += 1.0
    r = _stream(prov)
    assert r.level == OK and r.values["fps.delivered"] == 25.0 and r.values["fps.expected"] == 25.0
    assert r.values["timestamp.source"] == "ptp_chunk" and r.values["frame_age_s"] == 0.0


def test_stream_stall_is_an_error_after_the_grace_and_names_a_never_started_source():
    p, clock = _Pipe(), Clock()
    prov = _provider(p, clock)
    _stream(prov)
    clock.t += 4.0
    assert _stream(prov).level == OK, "inside the grace window"
    clock.t += 2.0
    r = _stream(prov)
    assert r.level == ERROR and r.message == "no frames yet for 6s"
    p.frames = 5
    clock.t += 1.0
    assert _stream(prov).level == OK, "frames flowing again"
    clock.t += 6.0
    r = _stream(prov)
    assert r.level == ERROR and r.message == "no frames for 6s"


def test_a_slow_camera_gets_three_frame_intervals_before_a_stall():
    p, clock = _Pipe(), Clock()
    p.fps = 0.25   # one frame every 4 s -> 12 s grace
    prov = _provider(p, clock)
    _stream(prov)
    clock.t += 11.0
    assert _stream(prov).level == OK
    clock.t += 2.0
    assert _stream(prov).level == ERROR


def test_reconnecting_is_an_error_and_a_loss_is_held_as_a_warning():
    p, clock = _Pipe(), Clock()
    prov = _provider(p, clock)
    _stream(prov)
    p.reconnecting = True
    assert _stream(prov).message == "source disconnected; reconnecting"
    p.reconnecting = False
    p.frames, p.gaps, p.missing = 30, 1, 3
    clock.t += 1.0
    r = _stream(prov)
    assert r.level == WARN and r.message == "3 frame(s) lost in the last 10s"
    p.frames, p.enq = 60, 1
    clock.t += 1.0
    assert _stream(prov).message == "4 frame(s) lost in the last 10s", "losses inside the hold accumulate"
    for _ in range(11):
        p.frames += 25
        clock.t += 1.0
        r = _stream(prov)
    assert r.level == OK, "the hold expires"


def test_a_paused_playback_is_idle_not_stalled():
    p, clock = _Pipe(), Clock()
    pb = type("PB", (), {"state": "paused"})()
    prov = _provider(p, clock, playback=pb)
    _stream(prov)
    clock.t += 60.0
    r = _stream(prov)
    assert r.level == OK and r.message == "playback paused" and r.values["playback.state"] == "paused"


def _recording(prov):
    return next(r for r in prov.poll() if r.component == "recording")


def test_recording_levels():
    p, clock = _Pipe(), Clock()
    with tempfile.TemporaryDirectory() as tmp:
        prov = _provider(p, clock, out_dir=os.path.join(tmp, "not", "yet", "created"))
        r = _recording(prov)
        assert r.level == OK and r.message == "inactive"
        assert r.values["disk.free_gb"] > 0 and 0 < r.values["disk.free_pct"] <= 100, \
            "a dir that doesn't exist yet is measured on the filesystem it will land on"
        p.state, p.recording = "active", {"frames": 100, "segments": 2, "encoder": "ffv1", "error": None,
                                          "output_dir": tmp}
        r = _recording(prov)
        assert r.level == OK and r.message == "recording" and r.values["frames"] == 100
        p.recording = dict(p.recording, error="disk full")
        r = _recording(prov)
        assert r.level == ERROR and r.message == "session error: disk full"
        p.state, p.recording, p.last_error = "inactive", None, "recording session ended on an error"
        r = _recording(prov)
        assert r.level == WARN and r.message == "recording session ended on an error"
    p.recording_enabled = False
    r = _recording(prov)
    assert r.level == OK and r.message == "disabled" and "disk.free_pct" not in r.values


# ---- the monitor ---------------------------------------------------------------------
class _Flaky:
    name = "flaky"
    components = ("camera",)

    def __init__(self):
        self.fail = False
        self.n = 0

    def poll(self):
        self.n += 1
        if self.fail:
            raise RuntimeError("link down")
        return [Report("camera", OK, "40 C", hardware_id="hw", values={"temp.device_c": 40.0 + self.n})]


def test_a_failing_provider_holds_its_last_values_then_goes_stale():
    clock, prov = Clock(), _Flaky()
    m = HealthMonitor([prov], instance="c", clock=clock, stale_after_s=5.0, wall_ns=lambda: 7)
    first = m.poll_once()
    _assert_ros_mappable(first)
    prov.fail = True
    clock.t += 3.0
    held = m.poll_once()
    assert held["status"] == first["status"], "brief failure: last good values, same level"
    clock.t += 3.0
    stale = m.poll_once()
    [st] = stale["status"]
    assert st["level"] == STALE and st["message"] == "no data for 6s: link down" and st["values"] == {}
    _assert_ros_mappable(stale)
    prov.fail = False
    assert m.poll_once()["level"] == OK


def test_a_provider_that_never_succeeded_goes_stale_after_the_same_window():
    clock, prov = Clock(), _Flaky()
    prov.fail = True
    m = HealthMonitor([prov], instance="c", clock=clock, stale_after_s=5.0)
    assert m.poll_once()["status"][0]["level"] == STALE, "nothing to hold on the first poll"


def test_observers_windows_and_the_file():
    clock, prov = Clock(), _Flaky()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sock", "health.json")
        m = HealthMonitor([prov], instance="c", clock=clock, file_path=path)
        seen = []
        m.add_observer(seen.append)
        m.add_observer(lambda s: 1 / 0)   # a broken observer never stops the others
        m.poll_once()
        w = m.open_window()
        assert w.samples == 1, "a window opens with the conditions it opened in"
        m.poll_once()
        m.poll_once()
        summary = m.close_window(w)
        m.poll_once()
        assert summary["samples"] == 3 and len(seen) == 4
        with open(path) as f:
            assert json.load(f) == m.latest
        assert not os.path.exists(path + ".tmp")
        m.stop()
        assert not os.path.exists(path), "a clean stop removes the snapshot: it must not read as current"
    assert m.close_window(None) is None


def test_status_changes_are_logged_once():
    clock, prov = Clock(), _Flaky()
    m = HealthMonitor([prov], instance="c", clock=clock, stale_after_s=1.0)
    records = []
    h = logging.Handler()
    h.emit = records.append
    logger = logging.getLogger("cam_driver.health")
    saved = logger.level
    logger.addHandler(h)
    logger.setLevel(logging.DEBUG)
    try:
        m.poll_once()          # OK on the first poll: a quiet start
        prov.fail = True
        clock.t += 2
        m.poll_once()          # -> STALE
        clock.t += 1
        m.poll_once()          # still STALE: no new line
        prov.fail = False
        m.poll_once()          # -> OK
    finally:
        logger.removeHandler(h)
        logger.setLevel(saved)
    lines = [r.getMessage() for r in records if r.levelno >= logging.INFO]
    assert lines == ["health: c: camera OK -> STALE (no data for 2s: link down)",
                     "health: c: camera STALE -> OK (40 C)"], lines


def test_the_thread_polls_and_stops_promptly():
    prov = _Flaky()
    m = HealthMonitor([prov], instance="c", interval_s=0.01)
    polled = threading.Event()
    m.add_observer(lambda s: polled.set())
    m.start()
    assert polled.wait(2.0)
    m.stop()
    n = prov.n
    assert not m._thread and prov.n == n


# ---- genicam ------------------------------------------------------------------------
class _Node:
    def __init__(self, dev, name):
        self.dev, self.name = dev, name

    def get_value_as_string(self):
        self.dev.reads.append(self.name)
        v = self.dev.values[self.name]
        if isinstance(v, Exception):
            raise v
        if callable(v):
            return v()
        return v


class _Device:
    def __init__(self, values, selector=None):
        self.values = dict(values)
        self.selector = selector          # list of DeviceTemperatureSelector entries, or None
        self.current = None
        self.reads, self.commands, self.writes = [], [], []
        if selector:
            self.values["DeviceTemperatureSelector"] = lambda: self.current
            self.values["DeviceTemperature"] = lambda: {"Sensor": "48.5", "Mainboard": "39.25"}[self.current]

    def get_feature(self, name):
        return _Node(self, name) if name in self.values else None

    def dup_available_enumeration_feature_values_as_strings(self, name):
        assert name == "DeviceTemperatureSelector"
        return list(self.selector)

    def set_string_feature_value(self, name, value):
        self.writes.append((name, value))
        self.current = value

    def execute_command(self, name):
        self.commands.append(name)


class _Stream:
    def get_n_infos(self):
        return 2

    def get_info_name(self, i):
        return ["n_failures", "n_missing_packets"][i]

    def get_info_type(self, i):
        return "<GType guint64 (44)>"

    def get_info_uint64(self, i):
        return [3, 7][i]


class _Cam:
    def __init__(self, dev, stream=None):
        self.device, self.stream = dev, stream
        self.hardware_id = "FLIR Blackfly S sn 123"
        self.control_lost = False


def _genicam(cam, **kw):
    return GenicamProvider(lambda: cam, **kw)


def test_parse_and_snake():
    assert parse_value("41") == 41 and parse_value("41.5") == 41.5 and parse_value("Slave", None) == "Slave"
    assert parse_value("125000000", 8e-6) == 1000.0
    assert parse_value("true") is True and parse_value("Critical") == "Critical"
    assert snake("Mainboard") == "mainboard" and snake("FPGA Core") == "fpga_core" and snake("SensorBoard") == "sensor_board"


def test_genicam_reads_what_the_camera_has_and_nothing_else():
    dev = _Device({"DeviceTemperature": "45.5", "DeviceUptime": "3600", "GevLinkSpeed": "1000",
                   "PtpStatus": "Slave", "PtpDataSetLatch": "", "PowerSupplyVoltage": "11.9"})
    [cam, stream] = _genicam(_Cam(dev, _Stream()), ptp_expected=True).poll()
    assert cam.component == "camera" and cam.level == OK and cam.message == "45.5 C"
    assert cam.hardware_id == "FLIR Blackfly S sn 123"
    assert cam.values == {"temp.device_c": 45.5, "uptime_s": 3600, "link.speed_mbps": 1000,
                          "ptp.state": "Slave", "supply.voltage_v": 11.9}
    assert dev.commands == ["PtpDataSetLatch"], "SFNC latches the PTP data set before it is read"
    assert stream.component == "stream" and stream.values == {"aravis.n_failures": 3, "aravis.n_missing_packets": 7}


def test_genicam_temperature_selector_names_each_reading():
    dev = _Device({}, selector=["Sensor", "Mainboard"])
    [cam] = _genicam(_Cam(dev)).poll()
    assert cam.values == {"temp.sensor_c": 48.5, "temp.mainboard_c": 39.25}
    assert cam.message == "48.5 C", "the hottest reading is the headline"
    assert dev.writes == [("DeviceTemperatureSelector", "Sensor"), ("DeviceTemperatureSelector", "Mainboard")]
    single = _Device({}, selector=["Sensor"])
    single.current = "Sensor"
    [cam] = _genicam(_Cam(single)).poll()
    assert cam.values == {"temp.sensor_c": 48.5} and single.writes == [], "one entry: no selector writes"


def test_genicam_sfnc_link_speed_is_bytes_per_second_and_config_features_win():
    dev = _Device({"DeviceLinkSpeed": "125000000", "LensTemp": "30", "DeviceUptime": "5"})
    [cam] = _genicam(_Cam(dev), features={"temp.lens_c": "LensTemp", "uptime_s": "Missing"}).poll()
    assert cam.values == {"link.speed_mbps": 1000.0, "temp.lens_c": 30}, "a configured name REPLACES the default"


def test_genicam_levels_ptp_and_vendor_temperature_state():
    dev = _Device({"PtpStatus": "Uncalibrated", "TemperatureState": "Critical"})
    [cam] = _genicam(_Cam(dev), ptp_expected=True).poll()
    assert cam.level == WARN and cam.message == "camera reports temperature Critical; PTP not locked (Uncalibrated)"
    [cam] = _genicam(_Cam(_Device({"PtpStatus": "Uncalibrated"})), ptp_expected=False).poll()
    assert cam.level == OK, "PTP state is only judged when the config asked for PTP"
    [cam] = _genicam(_Cam(_Device({"TemperatureState": "Error"}))).poll()
    assert cam.level == ERROR


def test_genicam_read_errors_skip_a_value_and_all_failing_raises():
    dev = _Device({"DeviceTemperature": "40", "DeviceUptime": RuntimeError("timeout")})
    [cam] = _genicam(_Cam(dev)).poll()
    assert cam.values == {"temp.device_c": 40, "read_errors": 1}
    dead = _Device({"DeviceTemperature": RuntimeError("timeout"), "DeviceUptime": RuntimeError("timeout")})
    try:
        _genicam(_Cam(dead)).poll()
    except RuntimeError as e:
        assert "all 2 feature reads failed" in str(e)
    else:
        raise AssertionError("a dead control channel must raise so the monitor can go STALE")


def test_genicam_does_not_touch_the_device_while_reconnecting_or_disconnected():
    dev = _Device({"DeviceTemperature": "40"})
    [r] = _genicam(_Cam(dev), busy=lambda: True).poll()
    assert r.level == STALE and r.message == "camera reconnecting" and dev.reads == []
    cam = _Cam(dev)
    cam.control_lost = True
    [r] = _genicam(cam).poll()
    assert r.level == STALE and r.message == "camera not connected" and dev.reads == []
    [r] = GenicamProvider(lambda: None).poll()
    assert r.level == STALE
    [cam] = _genicam(_Cam(_Device({}))).poll()
    assert cam.level == OK and cam.message == "no health features on this camera" and cam.values == {}


def test_genicam_stops_reading_when_the_monitor_stops():
    stop = threading.Event()
    dev = _Device({"DeviceTemperature": lambda: stop.set() or "40", "DeviceUptime": "1"})
    [cam] = _genicam(_Cam(dev), stop_event=stop).poll()
    assert dev.reads == ["DeviceTemperature"], "no further reads after the stop"


# ---- from_config + the config section ---------------------------------------------
class _Source:
    def __init__(self, gc=None):
        self._gc = gc
        self.playback = None
        self.active_timestamp_source = "system"

    def genicam(self):
        return self._gc


class _Lifecycle:
    def descriptor(self):
        return _Pipe().descriptor()


class _PipeObj:
    def __init__(self, source):
        self.source = source
        self.frame_rate = 25.0
        self.reconnecting = False


def _names(m):
    return [p.name for p in m.providers]


def test_from_config_adds_genicam_only_for_a_genicam_source():
    cfg = parse_config({"camera": {"type": "gige"}})
    m = HealthMonitor.from_config(cfg, instance="c", lifecycle=_Lifecycle(), pipeline=_PipeObj(_Source(_Cam(_Device({})))))
    assert _names(m) == ["pipeline", "genicam"]
    assert m.providers[1]._ptp_expected, "gige defaults: ptp_chunk + ptp_enable"
    cfg = parse_config({"camera": {"type": "usb"}})
    assert _names(HealthMonitor.from_config(cfg, instance="c", lifecycle=_Lifecycle(),
                                            pipeline=_PipeObj(_Source(None)))) == ["pipeline"]
    cfg = parse_config({"camera": {"type": "gige"}, "health": {"providers": {"genicam": {"enabled": False}}}})
    assert _names(HealthMonitor.from_config(cfg, instance="c", lifecycle=_Lifecycle(),
                                            pipeline=_PipeObj(_Source(_Cam(_Device({})))))) == ["pipeline"]
    cfg = parse_config({"camera": {"type": "rtsp"}, "health": {"providers": {"genicam": {"enabled": True}}}})
    assert _names(HealthMonitor.from_config(cfg, instance="c", lifecycle=_Lifecycle(),
                                            pipeline=_PipeObj(_Source(None)))) == ["pipeline"]
    snap = HealthMonitor.from_config(parse_config({}), instance="c", lifecycle=_Lifecycle(),
                                     pipeline=_PipeObj(_Source(_Cam(_Device({"DeviceTemperature": "40"}))))).poll_once()
    _assert_ros_mappable(snap)
    assert [s["name"] for s in snap["status"]] == ["c: stream", "c: recording", "c: camera"]


def test_health_config_defaults_and_file():
    cfg = parse_config({})
    h = cfg.health
    assert h.enabled and h.interval_s == 1.0 and h.stale_after_s == 5.0 and h.providers == {} and h.limits == {}
    assert health_file(cfg) == "/tmp/cam/health.json", "beside the transport sockets"
    assert health_file(parse_config({"health": {"file": "/x/h.json"}})) == "/x/h.json"
    assert health_file(parse_config({"health": {"write_file": False}})) == ""
    cfg = parse_config({"health": {"limits": {"temp.sensor_c": {"warn_above": 65, "error_above": 75.5}},
                                   "providers": {"genicam": {"enabled": "AUTO", "features": {"temp.lens_c": "LensT"}}}}})
    assert cfg.health.limits == {"temp.sensor_c": {"warn_above": 65.0, "error_above": 75.5}}
    assert cfg.health.providers == {"genicam": {"enabled": "auto", "features": {"temp.lens_c": "LensT"}}}
    assert parse_config({"health": {"providers": None, "limits": None}}).health.providers == {}


def test_health_config_refusals_name_the_key():
    bad = [
        ({"interval_s": 0}, "health.interval_s: must be > 0"),
        ({"providers": {"bosun": {}}}, "health.providers.bosun: unknown provider"),
        ({"providers": {"genicam": {"enabled": "sometimes"}}}, "health.providers.genicam.enabled"),
        ({"providers": {"genicam": {"features": {"t": 5}}}}, "health.providers.genicam.features"),
        ({"limits": {"temp.sensor_c": 65}}, "health.limits.temp.sensor_c: expected a map"),
        ({"limits": {"temp.sensor_c": {"warn_over": 65}}}, "health.limits.temp.sensor_c.warn_over: unknown bound"),
        ({"limits": {"temp.sensor_c": {"warn_above": "hot"}}}, "health.limits.temp.sensor_c.warn_above: expected a number"),
        ({"limits": {"temp.sensor_c": {"warn_above": True}}}, "expected a number"),
    ]
    for raw, msg in bad:
        try:
            parse_config({"health": raw})
        except ValueError as e:
            assert msg in str(e), (raw, str(e))
        else:
            raise AssertionError(f"accepted {raw!r}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
