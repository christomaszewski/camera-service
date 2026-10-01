r"""Service health: one snapshot shaped like ROS 2 `diagnostic_msgs/DiagnosticArray`, built from
pluggable PROVIDERS and handed to independent OUTPUTS (docs/HEALTH.md is the contract).

    providers (each optional)          HealthMonitor (own thread)          outputs
    pipeline  -> stream, recording  \                                  /  zenoh  fleet/<v>/svc/<i>/health
    genicam   -> camera (+ stream)   >  merge -> limits -> snapshot  -->   health.json on the socket volume
    ...                             /                                  \  recording-session summary + log

A provider returns Reports: (component, level, message, hardware_id, values). Reports for the same
component merge into ONE status entry named "<instance>: <component>"; the configured limits then
raise its level from the values. Levels are ROS's: OK=0 WARN=1 ERROR=2 STALE=3. `values` stay FLAT
scalars so every entry maps losslessly onto DiagnosticStatus.values (string key/value pairs) and a
zenoh->ROS relay can be added later without touching a producer.

Threading: the monitor polls on its OWN thread. A device read (a GigE GVCP round trip) can block for
seconds when the camera is going away, so it must never run on the GLib main loop, where the
lifecycle transitions and the zenoh change_state replies are served. Observers (the zenoh publisher)
are called on this thread. Recording windows are opened/closed from the main loop under a lock.

Pure Python -- no gi, no zenoh -- so every rule here is unit-tested in CI with stub providers.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SERVICE = "camera-service"

# diagnostic_msgs/DiagnosticStatus levels -- the numbers ARE the ROS byte values.
OK, WARN, ERROR, STALE = 0, 1, 2, 3
LEVEL_NAMES = {OK: "OK", WARN: "WARN", ERROR: "ERROR", STALE: "STALE"}

LIMIT_KEYS = ("warn_above", "error_above", "warn_below", "error_below")
# Deliberately short: a default threshold has to be right for EVERY camera. Temperature limits are
# per camera model, so none ship; the recording disk is the one universal failure worth a default.
DEFAULT_LIMITS = {
    "disk.free_pct": {"warn_below": 10.0, "error_below": 2.0},
}

LOSS_HOLD_S = 10.0   # a frame loss keeps `stream` at WARN this long, so a 1 Hz viewer can't miss it


def health_key(vehicle: str, instance: str) -> str:
    return f"fleet/{vehicle}/svc/{instance}/health"


# ---- reports + merge ------------------------------------------------------------
@dataclass
class Report:
    component: str                     # "camera" | "stream" | "recording" | ...
    level: int = OK
    message: str = ""
    hardware_id: str = ""
    values: Dict[str, object] = field(default_factory=dict)


def _scalar(v):
    """The contract: values are flat scalars. JSON has no NaN/Infinity (a browser's JSON.parse
    rejects the whole message), so a non-finite float becomes null."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return str(v)


def _numeric(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def evaluate_limits(values: dict, limits: dict):
    """(level, [messages]) for the values that cross a configured limit."""
    level, msgs = OK, []
    for key, rule in limits.items():
        v = values.get(key)
        if not _numeric(v):
            continue
        for bound, lvl, crossed in (("error_above", ERROR, lambda x, t: x > t),
                                    ("error_below", ERROR, lambda x, t: x < t),
                                    ("warn_above", WARN, lambda x, t: x > t),
                                    ("warn_below", WARN, lambda x, t: x < t)):
            t = rule.get(bound)
            if t is not None and crossed(v, t):
                level = max(level, lvl)
                msgs.append(f"{key} {v:g} {'>' if bound.endswith('above') else '<'} {t:g}")
                break   # the worst crossed bound for this key is enough
    return level, msgs


def merge(reports: List[Report], instance: str, limits: dict) -> List[dict]:
    """Group reports by component (first-seen order), apply limits, return the status entries."""
    order, by = [], {}
    for r in reports:
        if r.component not in by:
            order.append(r.component)
            by[r.component] = []
        by[r.component].append(r)
    out = []
    for comp in order:
        rs = by[comp]
        values = {}
        for r in rs:
            for k, v in r.values.items():
                values[k] = _scalar(v)
        level = max(r.level for r in rs)
        msgs = [r.message for r in rs if r.message and r.level == level and r.level != OK]
        lim_level, lim_msgs = evaluate_limits(values, limits)
        if lim_level > level:
            level, msgs = lim_level, lim_msgs + msgs
        elif lim_level == level and lim_msgs:
            msgs = msgs + lim_msgs
        if not msgs:
            msgs = [next((r.message for r in rs if r.message), LEVEL_NAMES[level])]
        out.append({
            "level": level,
            "name": f"{instance}: {comp}",
            "message": "; ".join(dict.fromkeys(msgs)),
            "hardware_id": next((r.hardware_id for r in rs if r.hardware_id), ""),
            "values": values,
        })
    return out


def build_snapshot(instance: str, stamp_unix_ns: int, status: List[dict],
                   service: str = SERVICE) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "service": service,
        "instance": instance,
        "stamp_unix_ns": int(stamp_unix_ns),
        "level": max((s["level"] for s in status), default=OK),
        "status": status,
    }


def resolve_limits(user: Optional[dict]) -> dict:
    """DEFAULT_LIMITS overlaid by the config's: a configured key replaces that key's whole rule."""
    out = {k: dict(v) for k, v in DEFAULT_LIMITS.items()}
    for k, v in (user or {}).items():
        out[k] = {b: float(t) for b, t in (v or {}).items() if b in LIMIT_KEYS and t is not None}
    return out


def write_json_atomic(path: str, obj) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


# ---- the per-session summary ------------------------------------------------------
class HealthWindow:
    """Accumulates the snapshots taken while a recording session is open, so the session's JSON
    says what the conditions WERE: per status entry the worst level seen, and first/last/min/max of
    every numeric value (last only, for strings)."""

    def __init__(self):
        self.samples = 0
        self.from_ns: Optional[int] = None
        self.to_ns: Optional[int] = None
        self._status: Dict[str, dict] = {}

    def add(self, snap: dict) -> None:
        self.samples += 1
        if self.from_ns is None:
            self.from_ns = snap["stamp_unix_ns"]
        self.to_ns = snap["stamp_unix_ns"]
        for st in snap["status"]:
            acc = self._status.setdefault(st["name"], {"worst_level": OK, "worst_message": None,
                                                       "hardware_id": st.get("hardware_id", ""),
                                                       "values": {}})
            if acc["worst_message"] is None or st["level"] > acc["worst_level"]:
                acc["worst_level"], acc["worst_message"] = st["level"], st["message"]
            for k, v in st["values"].items():
                if _numeric(v):
                    s = acc["values"].get(k)
                    if not isinstance(s, dict):
                        acc["values"][k] = {"first": v, "last": v, "min": v, "max": v}
                    else:
                        s["last"], s["min"], s["max"] = v, min(s["min"], v), max(s["max"], v)
                elif v is not None:
                    acc["values"][k] = v

    def summary(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "samples": self.samples,
                "from_unix_ns": self.from_ns, "to_unix_ns": self.to_ns,
                "status": self._status}


# ---- the pipeline provider (every config has one) -----------------------------------
class PipelineProvider:
    """`stream` (is capture flowing, and faithfully?) and `recording` (lifecycle state, the open
    session, the disk under it) -- from counters the core already keeps. Works for every source
    type, live or playback, with no device access at all."""

    name = "pipeline"
    components = ("stream", "recording")

    def __init__(self, descriptor: Callable[[], dict], *, frame_rate: Callable[[], float],
                 reconnecting: Callable[[], bool], playback=None,
                 timestamp_source: Callable[[], str] = lambda: "",
                 output_dir: str = "", stall_after_s: float = 5.0,
                 loss_hold_s: float = LOSS_HOLD_S, clock: Callable[[], float] = time.monotonic):
        self._descriptor = descriptor
        self._frame_rate = frame_rate
        self._reconnecting = reconnecting
        self._playback = playback
        self._ts_source = timestamp_source
        self._output_dir = output_dir
        self._stall_after_s = stall_after_s
        self._loss_hold_s = loss_hold_s
        self._clock = clock
        self._prev = None            # (t, frames) at the previous poll
        self._last_frame_t = None    # when the frame counter last moved
        self._prev_loss = None       # (source_gaps, frames_missing, enqueue_failures) at the previous poll
        self._last_loss_t = None
        self._lost_recent = 0        # frames lost inside the hold window

    def poll(self) -> List[Report]:
        now = self._clock()
        d = self._descriptor()
        return [self._stream(now, d.get("health") or {}), self._recording(d)]

    # ---- stream ------------------------------------------------------------
    def _stream(self, now: float, h: dict) -> Report:
        frames = int(h.get("frames", 0))
        fps_exp = float(self._frame_rate() or 0.0)
        values = {"fps.expected": round(fps_exp, 3) if fps_exp else None}
        if self._prev is not None and now > self._prev[0]:
            values["fps.delivered"] = round((frames - self._prev[1]) / (now - self._prev[0]), 2)
        if self._last_frame_t is None or (self._prev is not None and frames > self._prev[1]):
            self._last_frame_t = now          # first poll = the start of the grace window
        self._prev = (now, frames)
        age = now - self._last_frame_t
        values["frame_age_s"] = round(age, 2)
        for k in ("frames", "source_gaps", "frames_missing", "enqueue_failures", "publish_drops",
                  "pts_rebases"):
            if k in h:
                values[k] = h[k]
        reconnecting = bool(self._reconnecting())
        values["reconnecting"] = reconnecting
        ts = self._ts_source()
        if ts:
            values["timestamp.source"] = ts

        loss = (int(h.get("source_gaps", 0)), int(h.get("frames_missing", 0)),
                int(h.get("enqueue_failures", 0)))
        if self._prev_loss is not None and loss != self._prev_loss:
            lost = (loss[1] - self._prev_loss[1]) + (loss[2] - self._prev_loss[2])
            if self._last_loss_t is None or now - self._last_loss_t > self._loss_hold_s:
                self._lost_recent = 0
            self._lost_recent += max(lost, 1)
            self._last_loss_t = now
        self._prev_loss = loss

        pb_state = getattr(self._playback, "state", None) if self._playback is not None else None
        if pb_state is not None:
            values["playback.state"] = pb_state
        rate = f"{values['fps.delivered']:g} fps" if "fps.delivered" in values else "starting"
        if pb_state is not None and pb_state != "playing":
            return Report("stream", OK, f"playback {pb_state}", values=values)
        if reconnecting:
            return Report("stream", ERROR, "source disconnected; reconnecting", values=values)
        stall_after = max(self._stall_after_s, 3.0 / fps_exp) if fps_exp > 0 else self._stall_after_s
        if age > stall_after:
            what = "no frames yet" if frames == 0 else "no frames"
            return Report("stream", ERROR, f"{what} for {age:.0f}s", values=values)
        if self._last_loss_t is not None and now - self._last_loss_t <= self._loss_hold_s:
            return Report("stream", WARN, f"{self._lost_recent} frame(s) lost in the last "
                                          f"{self._loss_hold_s:.0f}s", values=values)
        return Report("stream", OK, rate, values=values)

    # ---- recording ---------------------------------------------------------
    def _recording(self, d: dict) -> Report:
        enabled = bool(d.get("recording_enabled", False))
        state = d.get("state", "")
        values = {"state": state, "enabled": enabled}
        if not enabled:
            return Report("recording", OK, "disabled", values=values)
        rec = d.get("recording") or {}
        if rec:
            values.update({"frames": rec.get("frames"), "segments": rec.get("segments"),
                           "encoder": rec.get("encoder")})
        values.update(self._disk(rec.get("output_dir") or self._output_dir))
        if rec.get("error"):
            return Report("recording", ERROR, f"session error: {rec['error']}", values=values)
        if d.get("last_error"):
            return Report("recording", WARN, str(d["last_error"]), values=values)
        return Report("recording", OK, "recording" if state == "active" else state, values=values)

    @staticmethod
    def _disk(path: str) -> dict:
        p = path
        while p and not os.path.exists(p):     # not created yet: measure the filesystem it will land on
            parent = os.path.dirname(p.rstrip("/"))
            if parent == p:
                break
            p = parent
        try:
            st = os.statvfs(p or ".")
        except OSError:
            return {}
        if not st.f_blocks:
            return {}
        return {"disk.free_gb": round(st.f_bavail * st.f_frsize / 1e9, 2),
                "disk.free_pct": round(100.0 * st.f_bavail / st.f_blocks, 1)}


# ---- the monitor ----------------------------------------------------------------------
class HealthMonitor:
    """Polls every provider each interval, merges, applies limits, fans the snapshot out. Never
    raises into the service: a provider that throws keeps its last good reports until it has been
    silent for `stale_after_s`, then its components go STALE (ROS: "no data")."""

    def __init__(self, providers: list, *, instance: str, limits: Optional[dict] = None,
                 interval_s: float = 1.0, stale_after_s: float = 5.0, file_path: str = "",
                 service: str = SERVICE, clock: Callable[[], float] = time.monotonic,
                 wall_ns: Callable[[], int] = time.time_ns):
        self.providers = list(providers)
        self.instance = instance
        self.limits = resolve_limits(limits)
        self.interval_s = interval_s
        self.stale_after_s = stale_after_s
        self.file_path = file_path
        self.service = service
        self._clock = clock
        self._wall_ns = wall_ns
        self._observers: list = []
        self._latest: Optional[dict] = None
        self._last_ok: Dict[str, float] = {}
        self._last_reports: Dict[str, List[Report]] = {}
        self._started_t: Optional[float] = None
        self._levels: Dict[str, int] = {}
        self._windows: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._file_warned = False

    # ---- fan-out -----------------------------------------------------------
    def add_observer(self, fn: Callable[[dict], None]) -> None:
        """fn(snapshot) after every poll, on the health thread. Errors are logged, never raised."""
        self._observers.append(fn)

    @property
    def latest(self) -> Optional[dict]:
        return self._latest

    def open_window(self) -> HealthWindow:
        w = HealthWindow()
        with self._lock:
            self._windows.append(w)
            if self._latest is not None:
                w.add(self._latest)   # the conditions the session OPENED in
        return w

    def close_window(self, w: Optional[HealthWindow]) -> Optional[dict]:
        if w is None:
            return None
        with self._lock:
            if w in self._windows:
                self._windows.remove(w)
        return w.summary()

    # ---- one poll ----------------------------------------------------------
    def poll_once(self) -> dict:
        now = self._clock()
        if self._started_t is None:
            self._started_t = now
        reports: List[Report] = []
        for p in self.providers:
            name = getattr(p, "name", type(p).__name__)
            try:
                rs = list(p.poll() or [])
                self._last_ok[name] = now
                self._last_reports[name] = rs
                reports.extend(rs)
            except Exception as e:   # noqa: BLE001 -- a provider must never take health down
                silent = now - self._last_ok.get(name, self._started_t)
                if silent < self.stale_after_s and name in self._last_reports:
                    reports.extend(self._last_reports[name])
                else:
                    log.debug("health provider %s failed: %s", name, e)
                    reports.extend(Report(c, STALE, f"no data for {silent:.0f}s: {e}")
                                   for c in getattr(p, "components", ()))
        snap = build_snapshot(self.instance, self._wall_ns(),
                              merge(reports, self.instance, self.limits), self.service)
        with self._lock:
            self._latest = snap
            for w in self._windows:
                w.add(snap)
        self._log_changes(snap)
        self._write_file(snap)
        for fn in self._observers:
            try:
                fn(snap)
            except Exception as e:   # noqa: BLE001
                log.warning("health observer failed: %s", e)
        return snap

    def _log_changes(self, snap: dict) -> None:
        for st in snap["status"]:
            prev = self._levels.get(st["name"])
            lvl = st["level"]
            if prev == lvl:
                continue
            self._levels[st["name"]] = lvl
            if prev is None and lvl == OK:
                continue   # quiet start: only departures from OK (and recoveries) are news
            line = (f"health: {st['name']} {LEVEL_NAMES.get(prev, '-')} -> {LEVEL_NAMES[lvl]}"
                    f" ({st['message']})")
            if lvl == ERROR:
                log.error(line)
            elif lvl in (WARN, STALE):
                log.warning(line)
            else:
                log.info(line)

    def _write_file(self, snap: dict) -> None:
        if not self.file_path:
            return
        try:
            write_json_atomic(self.file_path, snap)
            self._file_warned = False
        except OSError as e:
            if not self._file_warned:
                log.warning("health: could not write %s: %s", self.file_path, e)
                self._file_warned = True

    # ---- the thread ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)
        self._thread.start()
        log.info("health: polling %s every %gs%s", ", ".join(getattr(p, "name", "?") for p in self.providers),
                 self.interval_s, f" -> {self.file_path}" if self.file_path else "")

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = self._clock()
            try:
                self.poll_once()
            except Exception:   # noqa: BLE001 -- the loop must survive anything
                log.exception("health: poll failed")
            self._stop.wait(max(0.0, self.interval_s - (self._clock() - t0)))

    @property
    def stop_event(self) -> threading.Event:
        """Providers poll this between device reads, so a stop never waits out a whole poll."""
        return self._stop

    @classmethod
    def from_config(cls, cfg, *, instance: str, lifecycle, pipeline, file_path: str = "") -> "HealthMonitor":
        """The monitor for this deploy: the pipeline provider always; genicam when the source is
        GenICam-backed; boson when the usb source names a flir-boson command channel. Each device
        provider can be switched off (`enabled: false`). cfg = config.AppConfig."""
        h = cfg.health
        source = pipeline.source

        def ts_source() -> str:
            try:
                return source.active_timestamp_source
            except Exception:   # noqa: BLE001 -- before configure(): nothing to report
                return ""

        providers = [PipelineProvider(
            lifecycle.descriptor, frame_rate=lambda: pipeline.frame_rate,
            reconnecting=lambda: pipeline.reconnecting, playback=getattr(source, "playback", None),
            timestamp_source=ts_source, output_dir=cfg.recording.output_dir,
            stall_after_s=h.stall_after_s)]
        monitor = cls(providers, instance=instance, limits=h.limits, interval_s=h.interval_s,
                      stale_after_s=h.stale_after_s, file_path=file_path)
        g = h.providers.get("genicam", {})
        enabled = g.get("enabled", "auto")
        if enabled is not False:
            if source.genicam() is not None:
                from .health_genicam import GenicamProvider   # lazy: health.py stays import-light
                gige = cfg.gige if cfg.camera.type == "gige" else None
                monitor.providers.append(GenicamProvider(
                    source.genicam, busy=lambda: pipeline.reconnecting,
                    ptp_expected=bool(gige and gige.timestamp_source == "ptp_chunk" and gige.ptp_enable),
                    features=g.get("features"), stop_event=monitor.stop_event))
            elif enabled is True:
                log.warning("health.providers.genicam is enabled but camera.type %s has no GenICam "
                            "device; skipping it", cfg.camera.type)
        b = h.providers.get("boson", {})
        enabled = b.get("enabled", "auto")
        if enabled is not False:
            usb = cfg.usb
            if cfg.camera.type == "usb" and not usb.fake and usb.control_protocol == "flir-boson":
                from .health_boson import BosonProvider
                monitor.providers.append(BosonProvider(usb.control_device, stop_event=monitor.stop_event))
            elif enabled is True:
                log.warning("health.providers.boson is enabled but the source has no "
                            "usb.control_protocol: flir-boson; skipping it")
        return monitor

    def stop(self, wait: bool = True, timeout_s: float = 2.0) -> None:
        """Stop polling. From a signal handler pass wait=False (a device read can block); the
        final call after the pipeline is down joins, then removes the file -- a clean stop must not
        leave a snapshot that reads as current."""
        self._stop.set()
        if not wait:
            return
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout_s)
        self._thread = None
        if t is None or not t.is_alive():   # never close a device a still-running poll is using
            for p in self.providers:
                close = getattr(p, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as e:   # noqa: BLE001
                        log.debug("health: closing provider %s: %s", getattr(p, "name", "?"), e)
        if self.file_path:
            try:
                os.unlink(self.file_path)
            except OSError:
                pass
