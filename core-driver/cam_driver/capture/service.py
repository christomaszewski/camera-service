"""The capture pipeline (select -> encode -> EXIF -> write -> reply) and its Zenoh adapter.

Keys (docs/CAPTURE.md), on the same `fleet/<vehicle>/svc/<instance>` base as the core's lifecycle
and health:

    fleet/<vehicle>/svc/<instance>/capture           liveliness token + queryable -> descriptor
    fleet/<vehicle>/svc/<instance>/capture/request   queryable: a trigger -> a reply once written
    fleet/<vehicle>/svc/<instance>/capture/events    publisher: one record per written capture

Request queries are answered from a worker thread, after the files are written, so a reply means
the capture exists. The queue is bounded (`queue`): a trigger that finds it full is refused at once
with `busy`. Like the core's control plane, the session is peer mode with a finite connect timeout,
everything zenoh is best-effort and retried, and the reader keeps buffering regardless.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import exif, tiff
from .encode import JPEG_ENCODERS, MONO16_MODES
from .frames import Frame, FrameRing
from .reader import TRANSPORTS, Delivered
from .request import CaptureRequest, RequestError, fix_at, parse_request
from .timebase import TimeBase

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SERVICE = "frame-capture"
DEFAULT_TOLERANCE_MS = 50.0       # until the frame interval is known
SINK_NAMES = ("file", "zenoh")
FILE_ROOTS = ("recordings", "store")
IMAGE_FORMATS = ("jpeg", "tiff")
_DEDUPE = 256


@dataclass
class CaptureSettings:
    """The plugin's `params:` (plugins: - name: frame-capture), validated with the key named."""
    buffer_s: float = 2.0
    buffer_max_mb: float = 256.0
    buffer_fps: Optional[float] = None     # keep at most this many frames a second (None = every frame)
    tolerance_ms: Optional[float] = None   # None = one frame interval (measured), else 50 ms
    wait_ms: float = 250.0
    quality: int = 90
    max_width: int = 0
    jpeg_encoder: str = "auto"             # auto | hw | cpu (encode.JPEG_ENCODERS)
    formats: List[str] = field(default_factory=lambda: ["jpeg"])   # jpeg | tiff (16-bit radiometric)
    mono16: str = "stretch"                # stretch | shift | fixed (16-bit -> the JPEG's 8 bits)
    mono16_range: Optional[List[int]] = None   # [lo, hi] raw values mapped to 0..255 for mono16: fixed
    camera_make: Optional[str] = None      # EXIF Make / Model: what photogrammetry tools key cameras on
    camera_model: Optional[str] = None
    focal_length_mm: Optional[float] = None    # EXIF FocalLength
    pixel_pitch_um: Optional[float] = None     # sensor pixel size -> EXIF FocalPlaneX/YResolution
    band_name: Optional[str] = None        # XMP Camera:BandName (e.g. LWIR for a thermal core)
    central_wavelength_nm: Optional[float] = None
    wavelength_fwhm_nm: Optional[float] = None
    exif_altitude: str = "msl"             # msl | ellipsoid: what EXIF GPSAltitude holds
    bayer_pattern: str = "auto"            # auto | none | rggb | grbg | gbrg | bggr
    time_basis: str = "auto"               # auto | capture | arrival
    time_scale: str = "auto"               # auto | utc | tai
    tai_utc_offset_s: object = "auto"
    max_latency_ms: float = 5000.0         # capture -> plugin receipt budget for the plausibility check
    queue: int = 4
    sinks: List[str] = field(default_factory=lambda: ["file"])
    file_dir: str = "keyframes"
    file_root: str = "recordings"          # recordings: <recording dir>/<file_dir>; store: <run>/store/<instance>/<file_dir>
    store_prefix: str = "fleet/{vehicle}/store/{run}/{instance}/keyframes"
    transport: str = "auto"

    @classmethod
    def from_params(cls, params: dict) -> "CaptureSettings":
        params = dict(params or {})
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(params) - known)
        if unknown:
            raise ValueError(f"frame-capture params: unknown key(s) {unknown} (known: {sorted(known)})")
        s = cls(**params)
        s.sinks = [s.sinks] if isinstance(s.sinks, str) else list(s.sinks or [])
        bad = [n for n in s.sinks if n not in SINK_NAMES]
        if bad or not s.sinks:
            raise ValueError(f"frame-capture params.sinks: expected a non-empty subset of {SINK_NAMES}, "
                             f"got {s.sinks!r}")
        if s.file_root not in FILE_ROOTS:
            raise ValueError(f"frame-capture params.file_root: expected one of {FILE_ROOTS}, got {s.file_root!r}")
        if s.file_root == "store" and "file" in s.sinks:
            parts = str(s.file_dir or "").strip("/").split("/")
            if str(s.file_dir).startswith("/") or any(not p or p.startswith((".", "@")) or set(p) & set("*$?#\\")
                                                      for p in parts):
                raise ValueError(f"frame-capture params.file_dir: with file_root: store it must be a relative "
                                 f"path of Zenoh-key-safe names (no '..', '*', '$', '?', '#'), got {s.file_dir!r}")
            if "zenoh" in s.sinks:
                raise ValueError("frame-capture params.sinks: with file_root: store the file sink already "
                                 "feeds the vehicle store (the same keys the zenoh sink would put); keep one")
        for name in ("buffer_s", "buffer_max_mb", "wait_ms", "max_latency_ms"):
            v = getattr(s, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or \
                    (name in ("buffer_s", "buffer_max_mb") and v == 0):
                raise ValueError(f"frame-capture params.{name}: expected a positive number, got {v!r}")
        if s.buffer_fps is not None and (isinstance(s.buffer_fps, bool) or not isinstance(s.buffer_fps, (int, float))
                                         or s.buffer_fps <= 0):
            raise ValueError(f"frame-capture params.buffer_fps: expected a positive number, got {s.buffer_fps!r}")
        if s.tolerance_ms is not None and (isinstance(s.tolerance_ms, bool) or float(s.tolerance_ms) < 0):
            raise ValueError(f"frame-capture params.tolerance_ms: expected >= 0, got {s.tolerance_ms!r}")
        if not isinstance(s.queue, int) or isinstance(s.queue, bool) or s.queue < 1:
            raise ValueError(f"frame-capture params.queue: expected an integer >= 1, got {s.queue!r}")
        if s.jpeg_encoder not in JPEG_ENCODERS:
            raise ValueError(f"frame-capture params.jpeg_encoder: expected one of {JPEG_ENCODERS}, "
                             f"got {s.jpeg_encoder!r}")
        if s.transport not in TRANSPORTS:
            raise ValueError(f"frame-capture params.transport: expected one of {TRANSPORTS}, got {s.transport!r}")
        s.formats = [s.formats] if isinstance(s.formats, str) else list(s.formats or [])
        if not s.formats or any(f not in IMAGE_FORMATS for f in s.formats):
            raise ValueError(f"frame-capture params.formats: expected a non-empty subset of {IMAGE_FORMATS}, "
                             f"got {s.formats!r}")
        if s.mono16 not in MONO16_MODES:
            raise ValueError(f"frame-capture params.mono16: expected one of {MONO16_MODES}, got {s.mono16!r}")
        if s.mono16 == "fixed" or s.mono16_range is not None:
            r = s.mono16_range
            if (not isinstance(r, (list, tuple)) or len(r) != 2
                    or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 65535 for v in r)
                    or r[0] >= r[1]):
                raise ValueError("frame-capture params.mono16_range: expected [lo, hi], integers 0..65535 "
                                 f"with lo < hi (required by mono16: fixed), got {r!r}")
            s.mono16_range = [int(r[0]), int(r[1])]
        for name in ("camera_make", "camera_model", "band_name"):
            v = getattr(s, name)
            if v is not None and (not isinstance(v, str) or not v or len(v) > 64
                                  or not all(32 <= ord(c) < 127 for c in v)):
                raise ValueError(f"frame-capture params.{name}: expected printable ASCII (<= 64 chars), got {v!r}")
        for name in ("focal_length_mm", "pixel_pitch_um", "central_wavelength_nm", "wavelength_fwhm_nm"):
            v = getattr(s, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
                raise ValueError(f"frame-capture params.{name}: expected a positive number, got {v!r}")
        if s.exif_altitude not in exif.ALT_MODES:
            raise ValueError(f"frame-capture params.exif_altitude: expected one of {exif.ALT_MODES}, "
                             f"got {s.exif_altitude!r}")
        s.bayer_pattern = str(s.bayer_pattern or "auto").lower()
        if s.bayer_pattern not in ("auto", "none", "rggb", "grbg", "gbrg", "bggr"):
            raise ValueError(f"frame-capture params.bayer_pattern: got {s.bayer_pattern!r}")
        return s


def _now_ns() -> int:
    return time.time_ns()


class CaptureService:
    """Buffers delivered frames and serves capture requests. Thread-safe: frames arrive on the
    reader thread, requests on the worker thread, the descriptor on zenoh threads."""

    def __init__(self, settings: CaptureSettings, ring: FrameRing, timebase: TimeBase, encoder, sinks,
                 instance: str, vehicle: str, run_id: Optional[str], clock: Callable[[], int] = _now_ns):
        self.settings = settings
        self.ring = ring
        self.timebase = timebase
        self.encoder = encoder
        self.sinks = list(sinks)
        self.instance = instance
        self.vehicle = vehicle
        self.run_id = run_id
        self._clock = clock
        self._lock = threading.Lock()
        self._written: "OrderedDict[tuple, dict]" = OrderedDict()
        self.captures = 0
        self.failures = 0
        self.last: Optional[dict] = None
        self.reader = None             # set by the runner, for the descriptor
        self._observers: List[Callable[[dict], None]] = []

    def add_observer(self, fn: Callable[[dict], None]) -> None:
        self._observers.append(fn)

    # ---- frames ------------------------------------------------------------------------
    def on_delivery(self, d: Delivered) -> None:
        utc_ns, basis = self.timebase.normalize(d.stamp_ns, d.arrival_ns)
        self.ring.push(Frame(d.frame_id, d.stamp_ns, utc_ns, basis, d.arrival_ns, d.source, d.fmt,
                             d.width, d.height, d.bayer, d.data))

    def default_tolerance_ns(self) -> int:
        if self.settings.tolerance_ms is not None:
            return int(float(self.settings.tolerance_ms) * 1e6)
        interval = self.ring.interval_ns()
        return interval if interval else int(DEFAULT_TOLERANCE_MS * 1e6)

    # ---- requests ----------------------------------------------------------------------
    def handle_payload(self, payload: Optional[bytes], parameters: str = "") -> dict:
        try:
            req = parse_request(payload, parameters)
        except RequestError as e:
            return {"ok": False, "error": "bad_request", "detail": str(e)}
        return self.capture(req)

    def capture(self, req: CaptureRequest) -> dict:
        tol_ns = int(req.tolerance_ms * 1e6) if req.tolerance_ms is not None else self.default_tolerance_ns()
        wait_s = (req.wait_ms if req.wait_ms is not None else self.settings.wait_ms) / 1e3
        frame, reason = self.ring.select(req.stamp_ns, tol_ns, wait_s)
        base_reply = {"id": req.id} if req.id is not None else {}
        if frame is None:
            self.failures += 1
            detail = {"tolerance_ms": round(tol_ns / 1e6, 3), **self.ring.stats()}
            return {**base_reply, "ok": False, "error": reason, "detail": detail}
        key = (frame.frame_id, frame.stamp_ns)
        with self._lock:
            previous = self._written.get(key)
        if previous is not None:
            return {**base_reply, **previous, "duplicate": True}
        reply = self._write(frame, req)
        reply = {**base_reply, **reply}
        if reply.get("ok"):
            with self._lock:
                self._written[key] = {k: v for k, v in reply.items() if k != "id"}
                while len(self._written) > _DEDUPE:
                    self._written.popitem(last=False)
                self.captures += 1
                self.last = reply
            for fn in self._observers:
                try:
                    fn(reply)
                except Exception as e:   # noqa: BLE001
                    log.debug("capture observer failed: %s", e)
        else:
            self.failures += 1
        return reply

    def _camera(self) -> exif.CameraInfo:
        st = self.settings
        return exif.CameraInfo(st.camera_make, st.camera_model, st.focal_length_mm, st.pixel_pitch_um)

    def _xmp(self, req: CaptureRequest, fix) -> Optional[bytes]:
        st, o = self.settings, req.orientation
        return exif.build_xmp(
            yaw=o.yaw_deg if o else None, pitch=o.pitch_deg if o else None, roll=o.roll_deg if o else None,
            xy_acc=fix.h_acc_m if fix else None, z_acc=fix.v_acc_m if fix else None,
            yaw_acc=o.yaw_acc_deg if o else None, pitch_acc=o.pitch_acc_deg if o else None,
            roll_acc=o.roll_acc_deg if o else None, band_name=st.band_name,
            central_wavelength_nm=st.central_wavelength_nm, wavelength_fwhm_nm=st.wavelength_fwhm_nm)

    def _images(self, frame: Frame, req: CaptureRequest, fix):
        """[(ext, bytes, mime)], {format: description}, [warnings] -- one entry per configured format
        this frame can be written as. Raises on an encode failure."""
        yaw = req.orientation.yaw_deg if req.orientation is not None else None
        gps_ns = fix.stamp_ns if fix is not None else None
        camera, xmp, alt = self._camera(), self._xmp(req, fix), self.settings.exif_altitude
        files, images, warnings = [], {}, []
        base = f"{frame.utc_ns:019d}"
        if "jpeg" in self.settings.formats:
            enc = self.encoder.encode(frame)
            app1 = exif.build_exif(frame.utc_ns, enc.width, enc.height, gps=fix, gps_utc_ns=gps_ns,
                                   yaw_deg=yaw, camera=camera, scale=enc.width / frame.width, alt_mode=alt)
            jpeg = exif.insert_exif(enc.jpeg, app1, xmp)
            files.append(("jpg", jpeg, "image/jpeg"))
            images["jpeg"] = {"file": base + ".jpg", "width": enc.width, "height": enc.height,
                              "quality": getattr(self.encoder, "quality", None),
                              "encoder": getattr(self.encoder, "backend", None), "bytes": len(jpeg)}
        if "tiff" in self.settings.formats:
            if frame.fmt not in tiff.TIFF_FORMATS or frame.bayer:
                warnings.append(f"tiff skipped: needs a mono plane ({', '.join(tiff.TIFF_FORMATS)}), "
                                f"got {frame.fmt}{' ' + frame.bayer + ' mosaic' if frame.bayer else ''}")
            else:
                ifd0, ex, gps = exif.exif_ifds(frame.utc_ns, frame.width, frame.height, fix, gps_ns, yaw,
                                               camera, 1.0, alt, container="tiff")
                tif = tiff.build_tiff(frame.fmt, frame.width, frame.height, frame.data, ifd0, ex, gps, xmp)
                files.append(("tif", tif, "image/tiff"))
                images["tiff"] = {"file": base + ".tif", "width": frame.width, "height": frame.height,
                                  "bits": tiff.TIFF_FORMATS[frame.fmt][0], "bytes": len(tif)}
        return files, images, warnings

    def _write(self, frame: Frame, req: CaptureRequest) -> dict:
        fix, gps_method, gps_dt_ms = (None, "none", None)
        if req.gps is not None:
            fix, gps_method, gps_dt_ms = fix_at(req.gps, frame.utc_ns, req.stamp_ns)
        try:
            files, images, warnings = self._images(frame, req, fix)
        except Exception as e:   # noqa: BLE001 -- refuse this capture, keep serving
            log.warning("capture: encode failed for frame %s: %s", frame.frame_id, e)
            return {"ok": False, "error": "encode_failed", "detail": str(e)}
        if not files:
            return {"ok": False, "error": "unsupported_format", "detail": "; ".join(warnings)}
        base = f"{frame.utc_ns:019d}"
        dt_ms = round((frame.utc_ns - req.stamp_ns) / 1e6, 3) if req.stamp_ns is not None else None
        sidecar = {
            "schema_version": SCHEMA_VERSION, "service": SERVICE, "vehicle_id": self.vehicle,
            "instance": self.instance, "run_id": self.run_id,
            "frame": {"frame_id": frame.frame_id, "utc_ns": frame.utc_ns, "basis": frame.basis,
                      "stamp_ns": frame.stamp_ns, "stamp_source": frame.source, "arrival_ns": frame.arrival_ns,
                      "format": frame.fmt, "bayer": frame.bayer, "width": frame.width, "height": frame.height},
            "images": images,
            "camera": {k: v for k, v in vars(self._camera()).items() if v is not None} or None,
            "request": req.to_dict(),
            "match": {"dt_ms": dt_ms},
            "gps": None if req.gps is None else {
                "given": req.gps.to_dict(), "method": gps_method, "dt_ms": gps_dt_ms,
                "applied": fix.to_dict(), "msl_alt_m": fix.msl_alt_m(),
                "ellipsoid_alt_m": fix.ellipsoid_alt_m(), "exif_altitude": self.settings.exif_altitude},
            "orientation": req.orientation.to_dict() if req.orientation is not None else None,
            "written_unix_ns": self._clock(),
        }
        side_bytes = json.dumps(sidecar, indent=1, sort_keys=True).encode("utf-8")
        results = {s.name: s.write(base, files, side_bytes) for s in self.sinks}
        ok = any(r.get("ok") for r in results.values())
        reply = {"ok": ok, "frame_id": frame.frame_id, "frame_stamp_ns": frame.utc_ns, "basis": frame.basis,
                 "raw_stamp_ns": frame.stamp_ns, "stamp_source": frame.source, "dt_ms": dt_ms,
                 "gps": gps_method, "gps_dt_ms": gps_dt_ms, "formats": sorted(images),
                 "bytes": sum(len(f[1]) for f in files), "duplicate": False, "sinks": results}
        if warnings:
            reply["warnings"] = warnings
        if not ok:
            reply["error"] = next(iter(results.values())).get("error", "write_failed")
        return reply

    # ---- descriptor --------------------------------------------------------------------
    def descriptor(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION, "service": SERVICE, "vehicle_id": self.vehicle,
            "instance": self.instance, "run_id": self.run_id,
            "buffer": {**self.ring.stats(), "span_s": self.settings.buffer_s,
                       "max_mb": self.settings.buffer_max_mb,
                       "interval_ms": (round(self.ring.interval_ns() / 1e6, 3)
                                       if self.ring.interval_ns() else None)},
            "reader": self.reader.describe() if self.reader is not None else None,
            "encoder": self.encoder.describe() if hasattr(self.encoder, "describe") else None,
            "time": {"basis": self.timebase.basis, "scale": self.timebase.scale,
                     "tai_offset_s": self.timebase.tai_offset_ns // 1_000_000_000},
            "defaults": {"tolerance_ms": round(self.default_tolerance_ns() / 1e6, 3),
                         "wait_ms": self.settings.wait_ms},
            "sinks": [s.describe() for s in self.sinks],
            "captures": self.captures, "failures": self.failures, "last": self.last,
        }


# ---- zenoh -----------------------------------------------------------------------------
def encode_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")


def capture_key(vehicle: str, instance: str) -> str:
    return f"fleet/{vehicle}/svc/{instance}/capture"


class ZenohCapture:
    """The capture keys on one peer-mode session; best-effort, retried, never raises."""

    def __init__(self, service: CaptureService, key_base: str, connect, queue_size: int = 4,
                 session_factory: Optional[Callable] = None, store_sinks=(), retry_s: float = 5.0):
        self.service = service
        self.key_base = key_base
        self.request_key = key_base + "/request"
        self.events_key = key_base + "/events"
        self._connect = [e for e in (connect or []) if e]
        self._open = session_factory or self._open_zenoh
        self._store_sinks = list(store_sinks)
        self._retry_s = retry_s
        self._zenoh = None
        self._session = None
        self._decls = []
        self._token = None
        self._events = None
        self._jobs: "queue.Queue" = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, name="capture-worker", daemon=True)
        self._retrier: Optional[threading.Thread] = None
        service.add_observer(self._publish_event)

    @property
    def active(self) -> bool:
        return self._token is not None

    def start(self) -> None:
        self._worker.start()
        if not self.advertise():
            log.info("capture: zenoh not reachable yet; retrying every %gs", self._retry_s)
            self._retrier = threading.Thread(target=self._retry_loop, name="capture-zenoh-retry", daemon=True)
            self._retrier.start()

    def _retry_loop(self) -> None:
        while not self._stop.wait(self._retry_s):
            if self.advertise():
                return

    def _open_zenoh(self, connect):
        import zenoh
        self._zenoh = zenoh
        try:
            zenoh.init_log_from_env_or("error")
        except Exception:   # noqa: BLE001
            pass
        conf = zenoh.Config()
        conf.insert_json5("mode", '"peer"')
        if connect:
            conf.insert_json5("connect/endpoints", json.dumps(list(connect)))
        conf.insert_json5("connect/timeout_ms", "2000")
        conf.insert_json5("connect/exit_on_failure", "false")
        return zenoh.open(conf)

    def advertise(self) -> bool:
        if self._session is not None:
            return self.active
        try:
            self._session = self._open(self._connect)
            self._decls.append(self._session.declare_queryable(self.key_base, self._on_descriptor))
            self._decls.append(self._session.declare_queryable(self.request_key, self._on_request))
            self._events = self._session.declare_publisher(self.events_key)
            for sink in self._store_sinks:
                sink.attach(self._session, self._zenoh)
            self._token = self._session.liveliness().declare_token(self.key_base)
            log.info("capture: serving %s (connect=%s)", self.key_base, self._connect or "scout")
            return True
        except Exception as e:   # noqa: BLE001
            log.warning("capture: zenoh advertise failed (%s); buffering continues", e)
            self._close_session()
            return False

    def _json(self):
        return self._zenoh.Encoding.APPLICATION_JSON if self._zenoh is not None else None

    def _reply(self, query, obj, key: str) -> None:
        try:
            query.reply(key, encode_json(obj), encoding=self._json())
        except Exception as e:   # noqa: BLE001
            log.warning("capture: reply failed: %s", e)

    def _on_descriptor(self, query) -> None:
        try:
            self._reply(query, self.service.descriptor(), self.key_base)
        except Exception as e:   # noqa: BLE001
            log.warning("capture: descriptor reply failed: %s", e)

    def _on_request(self, query) -> None:
        """Zenoh thread: queue the job (the worker replies when it is written) or refuse now."""
        try:
            payload = getattr(query, "payload", None)
            raw = None
            if payload is not None:
                raw = payload.to_bytes() if hasattr(payload, "to_bytes") else bytes(payload)
            params = getattr(query, "parameters", "")
            self._jobs.put_nowait((query, raw, str(params) if params is not None else ""))
        except queue.Full:
            self._reply(query, {"ok": False, "error": "busy",
                                "detail": f"{self._jobs.maxsize} capture(s) already queued"}, self.request_key)
        except Exception as e:   # noqa: BLE001
            self._reply(query, {"ok": False, "error": "internal_error", "detail": str(e)}, self.request_key)

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            query, raw, params = job
            try:
                reply = self.service.handle_payload(raw, params)
            except Exception as e:   # noqa: BLE001
                log.exception("capture: request failed")
                reply = {"ok": False, "error": "internal_error", "detail": str(e)}
            self._reply(query, reply, self.request_key)
            del query, job      # a zenoh query is finalized when the last reference goes

    def _publish_event(self, reply: dict) -> None:
        pub = self._events
        if pub is None:
            return
        try:
            pub.put(encode_json({"instance": self.service.instance, **reply}), encoding=self._json())
        except Exception as e:   # noqa: BLE001
            log.debug("capture: event publish failed: %s", e)

    def _close_session(self) -> None:
        for sink in self._store_sinks:
            sink.detach()
        for obj in [self._token, self._events] + self._decls:
            try:
                if obj is not None:
                    obj.undeclare()
            except Exception:   # noqa: BLE001
                pass
        self._token, self._events, self._decls = None, None, []
        try:
            if self._session is not None:
                self._session.close()
        except Exception:   # noqa: BLE001
            pass
        self._session = None

    def close(self) -> None:
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(2.0)
        self._close_session()
