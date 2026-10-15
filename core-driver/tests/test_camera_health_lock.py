"""Shared Aravis access under health/control races; no physical camera required."""
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.camera import GigECamera
from cam_driver.config import GigeConfig
from cam_driver.health import ERROR, OK, STALE, HealthMonitor
from cam_driver.health_genicam import GenicamProvider
from cam_driver.sources.gige import GigeSource


class Device:
    def __init__(self, read=lambda: "40"):
        self.read = read

    def get_feature(self, name):
        return SimpleNamespace(get_value_as_string=self.read) if name == "DeviceTemperature" else None


def camera(read=lambda: "40", control=lambda: None):
    cam = GigECamera(GigeConfig())
    cam.device = Device(read)
    cam.hardware_id = "test camera"
    cam.camera = SimpleNamespace(
        set_acquisition_mode=lambda mode: control(), start_acquisition=lambda: None,
        stop_acquisition=control, get_region=control, get_pixel_format_as_string=control)
    return cam


def run_thread(fn):
    errors = []
    def run():
        try:
            fn()
        except BaseException as e:
            errors.append(e)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    return worker, errors


@pytest.mark.parametrize("operation", ["start", "stop", "sensor_geometry", "pixel_format_string", "close"])
def test_control_and_release_wait_for_an_inflight_health_read(operation):
    entered, release, requested, controlled = (threading.Event() for _ in range(4))
    def read():
        entered.set()
        assert release.wait(3)
        assert not controlled.is_set(), "control overlapped the native feature read"
        return "40"
    cam = camera(read, controlled.set)
    monitor = HealthMonitor([GenicamProvider(lambda: cam)], instance="c")
    poller, poll_errors = run_thread(monitor.poll_once)
    actor = None
    try:
        assert entered.wait(2)
        def act():
            requested.set()
            getattr(cam, operation)()
            controlled.set()
        actor, control_errors = run_thread(act)
        assert requested.wait(2)
        assert not controlled.wait(0.05)
        assert cam.device is not None, "release must also wait for the read"
    finally:
        release.set()
        poller.join(3)
        if actor is not None:
            actor.join(3)
    assert not poller.is_alive() and not actor.is_alive()
    assert not poll_errors and not control_errors
    assert controlled.is_set()
    if operation == "close":
        assert cam.device is None


def test_health_skips_busy_control_without_blocking_or_refreshing_the_sample():
    entered, release = threading.Event(), threading.Event()
    def control():
        entered.set()
        assert release.wait(3)
    cam = camera(control=control)
    now = [100.0]
    monitor = HealthMonitor([GenicamProvider(lambda: cam)], instance="c", clock=lambda: now[0])
    first = monitor.poll_once()["status"][0]
    actor, errors = run_thread(cam.start)
    poller = None
    snapshots = []
    try:
        assert entered.wait(2)
        now[0] += 1
        poller, poll_errors = run_thread(lambda: snapshots.append(monitor.poll_once()))
        poller.join(0.5)
        assert not poller.is_alive(), "health waited for the control operation"
        row = snapshots[0]["status"][0]
        assert row["level"] == OK
        assert row["values"]["health.availability"] == "retrying"
        assert row["values"]["health.sample_id"] == first["values"]["health.sample_id"]
        assert row["values"]["health.sample_age_s"] == 1
    finally:
        release.set()
        actor.join(3)
        if poller is not None:
            poller.join(3)
    assert not errors and not poll_errors


def test_reconnect_between_reads_discards_the_mixed_device_poll(monkeypatch):
    cam = camera(read=lambda: "90")
    provider = GenicamProvider(lambda: cam)
    monitor = HealthMonitor([provider], instance="c", limits={"temp.device_c": {"error_above": 75}})
    original = monitor.poll_once()["status"][0]
    assert original["level"] == ERROR
    replacement = Device(lambda: "30")  # allocate before releasing the old device (distinct identity)
    original_has = provider._has
    replaced = []
    def swap_before_next_feature(feature):
        if feature == "DeviceUptime" and not replaced:
            # This boundary is outside every health transaction: close must proceed immediately.
            cam.close()
            with cam.io_lock:
                cam.device = replacement
            replaced.append(True)
        return original_has(feature)
    monkeypatch.setattr(provider, "_has", swap_before_next_feature)
    row = monitor.poll_once()["status"][0]
    assert replaced and row["level"] == STALE
    assert row["values"]["temp.device_c"] == 90 and row["values"]["health.last_level"] == ERROR
    assert row["values"]["health.sample_id"] == original["values"]["health.sample_id"]
    recovered = monitor.poll_once()["status"][0]
    assert recovered["level"] == OK and recovered["values"]["temp.device_c"] == 30


def test_disabling_stream_signals_allows_frame_callback_to_finish():
    source = GigeSource(GigeConfig())
    source.camera = camera(control=lambda: (0, 0, 640, 480))
    callbacks = []
    def disable(enabled):
        assert enabled is False
        worker, errors = run_thread(source.camera.sensor_geometry)
        worker.join(0.5)
        callbacks.append((not worker.is_alive(), errors))
        assert not worker.is_alive(), "stream shutdown held the lock needed by the frame callback"
    source.camera.stream = SimpleNamespace(set_emit_signals=disable)
    source.stop()
    # stop() contains callback exceptions, so preserve the verdict outside that handler too.
    assert callbacks == [(True, [])]


def test_stream_statistics_and_signal_control_do_not_overlap():
    entered, release, requested, controlled = (threading.Event() for _ in range(4))
    source = GigeSource(GigeConfig())
    source.camera = camera()
    def count():
        entered.set()
        assert release.wait(3)
        assert not controlled.is_set()
        return 0
    source.camera.stream = SimpleNamespace(get_n_infos=count,
                                           set_emit_signals=lambda enabled: controlled.set())
    provider = GenicamProvider(lambda: source.camera)
    monitor = HealthMonitor([provider], instance="c")
    poller, poll_errors = run_thread(monitor.poll_once)
    actor = None
    try:
        assert entered.wait(2)
        def stop():
            requested.set()
            source.stop()
        actor, control_errors = run_thread(stop)
        assert requested.wait(2)
        assert not controlled.wait(0.05)
    finally:
        release.set()
        poller.join(3)
        if actor is not None:
            actor.join(3)
    assert not poller.is_alive() and not actor.is_alive()
    assert controlled.is_set() and not poll_errors and not control_errors
