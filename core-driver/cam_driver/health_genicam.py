"""GenICam health provider: the camera's own temperature / uptime / supply / link / PTP state, read
over the control channel of any Aravis-backed source (GigE Vision today; USB3 Vision is the same
feature tree). docs/HEALTH.md "genicam".

Names follow the GenICam SFNC and are PROBED, never assumed: a camera that lacks a feature simply
doesn't report that value (the Aravis fake camera has none of them). Vendors that use other names are
covered by the config's `features:` map (value name -> feature name), which also adds new values.

Reads go through `node.get_value_as_string()` -- one call for every node type (float, integer,
enumeration, string, boolean) -- then parse. Each read is a GVCP round trip (~ms); a camera that is
going away can make one block for the GVCP timeout, which is why this runs on the health thread and
checks the stop event between reads. Nothing is polled while the pipeline is reconnecting: the
reconnect worker is replacing the device object underneath, and a health read holding a reference to
the OLD device would delay its control-privilege release (camera.GigECamera._release).

gi-free at import: the device is duck-typed, so the unit tests drive it with a stub.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Dict, List, Optional

from .health import ERROR, OK, STALE, WARN, Report

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

    def poll(self) -> List[Report]:
        if self._busy():
            return [Report("camera", STALE, "camera reconnecting")]
        cam = self._target()
        dev = getattr(cam, "device", None) if cam is not None else None
        if dev is None or getattr(cam, "control_lost", False):
            return [Report("camera", STALE, "camera not connected",
                           hardware_id=getattr(cam, "hardware_id", "") or "")]
        hw = getattr(cam, "hardware_id", "") or ""
        values, errors, reads = {}, [], 0

        if self._has(dev, PTP_LATCH) and any(self._has(dev, f) for f in ("PtpStatus", "PtpOffsetFromMaster")):
            reads += 1
            try:
                dev.execute_command(PTP_LATCH)
            except Exception as e:   # noqa: BLE001 -- GLib.Error; the reads below just see older data
                errors.append(f"{PTP_LATCH}: {e}")

        for key, value in self._temperatures(dev, errors):
            reads += 1
            if key is not None:
                values[key] = value
        for key, candidates in self._features.items():
            if self._stop.is_set():
                break
            for feature, scale in candidates:
                if not self._has(dev, feature):
                    continue
                reads += 1
                try:
                    values[key] = parse_value(dev.get_feature(feature).get_value_as_string(), scale)
                except Exception as e:   # noqa: BLE001 -- GLib.Error on a read: skip this value
                    errors.append(f"{feature}: {e}")
                break
        if reads and len(errors) >= reads:
            # Every read failed: the control channel is gone, not a missing feature. Let the monitor
            # hold the last good values, then go STALE.
            raise RuntimeError(f"all {reads} feature reads failed ({errors[0]})")
        reports = [self._camera_report(hw, values, errors)]
        stream = self._stream_stats(cam)
        if stream:
            reports.append(Report("stream", OK, values=stream))
        return reports

    # ---- pieces --------------------------------------------------------------
    @staticmethod
    def _has(dev, feature: str) -> bool:
        try:
            return dev.get_feature(feature) is not None
        except Exception:   # noqa: BLE001
            return False

    def _selector_entries(self, dev) -> list:
        key = id(dev)
        if self._selector_cache[0] == key:
            return self._selector_cache[1]
        entries = []
        if self._has(dev, TEMPERATURE_SELECTOR):
            try:
                fn = getattr(dev, "dup_available_enumeration_feature_values_as_strings", None) \
                    or getattr(dev, "get_available_enumeration_feature_values_as_strings")
                entries = list(fn(TEMPERATURE_SELECTOR) or [])
            except Exception as e:   # noqa: BLE001
                log.debug("health: %s entries unreadable: %s", TEMPERATURE_SELECTOR, e)
        self._selector_cache = (key, entries)
        return entries

    def _temperatures(self, dev, errors: list):
        """(value name, value) per temperature read -- (None, None) for a failed one. With a
        selector: one per entry, temp.<entry>_c (Sensor -> temp.sensor_c, Mainboard ->
        temp.mainboard_c); without one the single reading is temp.device_c."""
        if not self._has(dev, TEMPERATURE):
            return []
        out = []
        entries = self._selector_entries(dev)
        for entry in entries or [None]:
            if self._stop.is_set():
                break
            try:
                if entry is not None and len(entries) > 1:
                    dev.set_string_feature_value(TEMPERATURE_SELECTOR, entry)
                v = parse_value(dev.get_feature(TEMPERATURE).get_value_as_string())
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
        return Report("camera", level, "; ".join(msgs), hardware_id=hw, values=values)

    @staticmethod
    def _stream_stats(cam) -> dict:
        """Aravis's host-side stream counters (completed/failed buffers, underruns; a GVSP stream
        adds missing/resent packets), as aravis.<name>. Cumulative, like the drop counters."""
        stream = getattr(cam, "stream", None)
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
