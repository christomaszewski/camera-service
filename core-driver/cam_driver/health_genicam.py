"""GenICam health provider: the camera's own temperature / uptime / supply / link / PTP state, read
over the control channel of any Aravis-backed source (GigE Vision today; USB3 Vision is the same
feature tree). docs/HEALTH.md "genicam".

Names follow the GenICam SFNC and are PROBED, never assumed: a camera that lacks a feature simply
doesn't report that value (the Aravis fake camera has none of them). Vendors that use other names are
covered by the config's `features:` map (value name -> feature name), which also adds new values.

Reads go through `node.get_value_as_string()` -- one call for every node type (float, integer,
enumeration, string, boolean) -- then parse. Each read is a GVCP round trip (~ms); a camera that is
going away can make one block for the GVCP timeout, which is why this runs on the health thread and
checks the stop event between reads. Each device transaction shares the camera's io_lock with
acquisition and configuration; polling yields the lock and drops all Aravis references between
transactions. A concurrent reconnect invalidates the poll instead of mixing devices or keeping the
old control privilege alive. Busy control operations are skipped, never waited for by health.

gi-free at import: the device is duck-typed, so the unit tests drive it with a stub.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Dict, List, Optional

from .health import (ERROR, OK, WARN, ObservationUnavailable, Report, supply_power,
                     validate_measurements)

log = logging.getLogger(__name__)

# value name -> [(feature, scale)] tried in order; scale None = keep the string.
DEFAULT_FEATURES = {
    "uptime_s": [("DeviceUptime", 1)],
    "supply.voltage_v": [("PowerSupplyVoltage", 1)],
    "supply.current_a": [("PowerSupplyCurrent", 1)],
    "link.speed_mbps": [("GevLinkSpeed", 1), ("DeviceLinkSpeed", 8e-6)],   # SFNC DeviceLinkSpeed is bytes/s
    "ptp.state": [("PtpStatus", None), ("GevIEEE1588Status", None)],
    "ptp.offset_ns": [("PtpOffsetFromMaster", 1)],
    "temp.state": [("TemperatureState", None)],                             # Basler: Ok | Critical | Error
}
TEMPERATURE = "DeviceTemperature"
TEMPERATURE_SELECTOR = "DeviceTemperatureSelector"
PTP_LATCH = "PtpDataSetLatch"                  # SFNC: Ptp* status values are latched by this command
PTP_LOCKED = ("Slave", "Master")               # the same set camera.enable_ptp treats as locked
TEMP_STATE_LEVELS = {"ok": OK, "critical": WARN, "error": ERROR}


def snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(s)).lower()).strip("_")


def parse_value(s: str, scale=1):
    """GenICam string representation -> int | float | str (scaled when numeric and scale given)."""
    if scale is None:
        return s
    for kind in (int, float):
        try:
            v = kind(s)
        except (TypeError, ValueError):
            continue
        if scale != 1:
            v = round(v * scale, 6)
        return v
    lowered = str(s).strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    return s


class GenicamProvider:
    name = "genicam"
    components = ("camera",)

    def __init__(self, target: Callable[[], object], *, busy: Callable[[], bool] = lambda: False,
                 ptp_expected: bool = False, features: Optional[Dict[str, str]] = None,
                 stop_event: Optional[threading.Event] = None):
        """target() -> the source's GenICam handle (camera.GigECamera: .device, .stream,
        .hardware_id) or None when not connected. busy() -> True while the pipeline reconnects."""
        self._target = target
        self._busy = busy
        self._ptp_expected = ptp_expected
        self._features = {k: list(v) for k, v in DEFAULT_FEATURES.items()}
        for value_name, feature in (features or {}).items():
            self._features[value_name] = [(feature, 1)]   # a configured name wins over the defaults
        self._stop = stop_event or threading.Event()
        self._selector_cache = (None, None)   # (id(device), [entries]) -- probed once per device
        self._measurements = set()
        self._device_id = None
        self._status_values = {}

    def _device_call(self, fn, *, reset=False):
        """One transaction; fn must return only detached values, never Aravis objects."""
        if self._stop.is_set():
            raise RuntimeError("health stopped")
        if self._busy():
            raise ObservationUnavailable("camera reconnecting")
        cam = self._target()
        if cam is None:
            raise ObservationUnavailable("camera not connected")
        if not cam.io_lock.acquire(blocking=False):
            raise RuntimeError("camera control busy")
        try:
            if self._busy():
                raise ObservationUnavailable("camera reconnecting")
            dev = cam.device
            if dev is None or cam.control_lost:
                raise ObservationUnavailable("camera not connected")
            if self._device_id != id(dev):
                if not reset:
                    raise ObservationUnavailable("camera changed during health poll")
                self._measurements.clear()
                self._status_values.clear()
                self._selector_cache = (None, None)
                self._device_id = id(dev)
            return fn(dev, cam)
        finally:
            # Do not keep the old device alive across a reconnect after releasing the lock.
            dev = None
            cam.io_lock.release()

    def poll(self) -> List[Report]:
        hw = self._device_call(lambda dev, cam: cam.hardware_id or "", reset=True)
        values, errors, reads = {}, [], 0

        latch_failed = False
        if self._has(PTP_LATCH) and any(self._has(f) for f in ("PtpStatus", "PtpOffsetFromMaster")):
            reads += 1
            try:
                self._device_call(lambda dev, cam: dev.execute_command(PTP_LATCH))
            except ObservationUnavailable:
                raise
            except Exception as e:   # noqa: BLE001 -- the latched fields are unavailable this poll
                errors.append(f"{PTP_LATCH}: {e}")
                latch_failed = True

        for key, value in self._temperatures(errors):
            reads += 1
            if key is not None:
                values[key] = value
        for key, candidates in self._features.items():
            if self._stop.is_set():
                break
            for feature, scale in candidates:
                if not self._has(feature):
                    continue
                reads += 1
                try:
                    if latch_failed and feature in ("PtpStatus", "PtpOffsetFromMaster"):
                        raise RuntimeError("PTP latch failed")
                    values[key] = self._device_call(
                        lambda dev, cam: parse_value(dev.get_feature(feature).get_value_as_string(), scale))
                    if key in ("temp.state", "ptp.state"):
                        self._status_values[key] = values[key]
                except ObservationUnavailable:
                    raise
                except Exception as e:   # noqa: BLE001 -- GLib.Error on a read: skip this value
                    errors.append(f"{feature}: {e}")
                    if key in ("temp.state", "ptp.state"):
                        values[key] = self._status_values.get(key)
                        values[f"health.metric.{key}.state"] = "unavailable"
                break
        if reads and len(errors) >= reads:
            # Every read failed: the control channel is gone, not a missing feature. Let the monitor
            # hold the last good values, then go STALE.
            raise RuntimeError(f"all {reads} feature reads failed ({errors[0]})")
        self._measurements.update(k for k in values if k.startswith("temp.") and k.endswith("_c")
                                  or k in ("supply.voltage_v", "supply.current_a", "supply.power_w"))
        for k, candidates in self._features.items():
            metric = k.startswith("temp.") and k.endswith("_c") or k in ("supply.voltage_v", "supply.current_a", "supply.power_w")
            if metric and any(self._has(feature) for feature, _ in candidates):
                self._measurements.add(k)
        if self._has(TEMPERATURE):
            self._measurements.update(f"temp.{snake(entry)}_c" for entry in self._selector_entries())
            if not self._selector_entries():
                self._measurements.add("temp.device_c")
        validate_measurements(values, self._measurements)
        supply_power(values)
        if "supply.power_w" not in values and {"supply.voltage_v", "supply.current_a"} <= self._measurements:
            validate_measurements(values, ["supply.power_w"])
        reports = [self._camera_report(hw, values, errors)]
        stream = self._stream_stats(self._target())
        if stream:
            reports.append(Report("stream", OK, values=stream))
        return reports

    # ---- pieces --------------------------------------------------------------
    def _has(self, feature: str) -> bool:
        return self._device_call(lambda dev, cam: dev.get_feature(feature) is not None)

    def _selector_entries(self) -> list:
        key = self._device_id
        if self._selector_cache[0] == key:
            return self._selector_cache[1]
        entries = []
        if self._has(TEMPERATURE_SELECTOR):
            try:
                def read(dev, cam):
                    fn = getattr(dev, "dup_available_enumeration_feature_values_as_strings", None) \
                        or getattr(dev, "get_available_enumeration_feature_values_as_strings")
                    return list(fn(TEMPERATURE_SELECTOR) or [])
                entries = self._device_call(read)
            except ObservationUnavailable:
                raise
            except Exception as e:   # noqa: BLE001
                log.debug("health: %s entries unreadable: %s", TEMPERATURE_SELECTOR, e)
        self._selector_cache = (key, entries)
        return entries

    def _temperatures(self, errors: list):
        """(value name, value) per temperature read -- (None, None) for a failed one. With a
        selector: one per entry, temp.<entry>_c (Sensor -> temp.sensor_c, Mainboard ->
        temp.mainboard_c); without one the single reading is temp.device_c."""
        if not self._has(TEMPERATURE):
            return []
        out = []
        entries = self._selector_entries()
        for entry in entries or [None]:
            if self._stop.is_set():
                break
            try:
                def read(dev, cam):
                    if entry is not None and len(entries) > 1:
                        dev.set_string_feature_value(TEMPERATURE_SELECTOR, entry)
                    return parse_value(dev.get_feature(TEMPERATURE).get_value_as_string())
                v = self._device_call(read)
            except ObservationUnavailable:
                raise
            except Exception as e:   # noqa: BLE001
                errors.append(f"{TEMPERATURE}[{entry or ''}]: {e}")
                out.append((None, None))
                continue
            out.append((f"temp.{snake(entry)}_c" if entry is not None else "temp.device_c", v))
        return out

    def _camera_report(self, hw: str, values: dict, errors: list) -> Report:
        level, msgs = OK, []
        state = values.get("temp.state")
        if isinstance(state, str) and TEMP_STATE_LEVELS.get(state.lower(), OK) > OK:
            level = max(level, TEMP_STATE_LEVELS[state.lower()])
            msgs.append(f"camera reports temperature {state}")
        ptp = values.get("ptp.state")
        if self._ptp_expected and ptp is not None and ptp not in PTP_LOCKED:
            level = max(level, WARN)
            msgs.append(f"PTP not locked ({ptp})")
        temps = [v for k, v in values.items() if k.startswith("temp.") and k.endswith("_c")
                 and isinstance(v, (int, float))]
        if not msgs:
            msgs.append(f"{max(temps):g} C" if temps else ("OK" if values else "no health features on this camera"))
        if errors:
            values["read_errors"] = len(errors)
            log.debug("health: genicam read errors: %s", "; ".join(errors))
        if any(v == "unavailable" for k, v in values.items() if k.startswith("health.metric.")):
            level = max(level, WARN)
            msgs.append("measurement unavailable")
        return Report("camera", level, "; ".join(msgs), hardware_id=hw, values=values)

    @staticmethod
    def _stream_stats(cam) -> dict:
        """Aravis's host-side stream counters (completed/failed buffers, underruns; a GVSP stream
        adds missing/resent packets), as aravis.<name>. Cumulative, like the drop counters."""
        if cam is None or not cam.stream_lock.acquire(blocking=False):
            return {}
        try:
            return GenicamProvider._read_stream_stats(cam.stream)
        finally:
            cam.stream_lock.release()

    @staticmethod
    def _read_stream_stats(stream) -> dict:
        if stream is None:
            return {}
        out = {}
        try:
            for i in range(stream.get_n_infos()):
                t = stream.get_info_type(i)
                if "uint64" in str(getattr(t, "name", t)):
                    out[f"aravis.{stream.get_info_name(i)}"] = int(stream.get_info_uint64(i))
        except Exception:   # noqa: BLE001 -- older Aravis: the three classic counters
            try:
                done, failed, under = stream.get_statistics()
                out = {"aravis.n_completed_buffers": int(done), "aravis.n_failures": int(failed),
                       "aravis.n_underruns": int(under)}
            except Exception:   # noqa: BLE001
                return {}
        return out
