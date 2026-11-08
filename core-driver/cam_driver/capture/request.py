"""Capture triggers: parsing, validation, and moving a GPS fix to the chosen frame (docs/CAPTURE.md).

A trigger is a JSON object on `fleet/<vehicle>/svc/<instance>/capture/request`:

    {"v": 1, "stamp_ns": 1762610400123456789, "tolerance_ms": 30, "wait_ms": 200,
     "gps": {"lat": 40.44, "lon": -79.94, "alt_m": 310.2, "alt_ref": "ellipsoid", "geoid_sep_m": -33.9,
             "stamp_ns": 1762610400120000000, "vel_enu": [1.2, 4.8, 0.0], "h_acc_m": 0.02, "v_acc_m": 0.03},
     "orientation": {"yaw_deg": 84.0, "pitch_deg": -2.1, "roll_deg": 0.4},
     "tag": "overlap", "id": "req-17", "meta": {...}}

Only `stamp_ns` matters for choosing the frame, and it is optional: without it the newest frame is
taken (a manual snapshot). Everything else is carried into the outputs. A payload-less get may use
selector parameters for the simple fields (`...?stamp_ns=...;tag=manual`).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

MAX_REQUEST_BYTES = 16384
MAX_EXTRAPOLATION_S = 1.0         # a fix further than this from the frame is written as given
ALT_REFS = ("ellipsoid", "msl")

# WGS-84
_A = 6378137.0
_E2 = 6.69437999014e-3


@dataclass(frozen=True)
class GpsFix:
    lat: float
    lon: float
    alt_m: Optional[float] = None
    alt_ref: str = "ellipsoid"          # what alt_m is measured from (GNSS reports ellipsoid height)
    geoid_sep_m: Optional[float] = None  # geoid separation N (ellipsoid - MSL), e.g. NovAtel "undulation"
    stamp_ns: Optional[int] = None      # when the fix applies (UTC); None = the request's stamp_ns
    vel_enu: Optional[Tuple[float, float, float]] = None   # m/s east, north, up
    h_acc_m: Optional[float] = None
    v_acc_m: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"lat": self.lat, "lon": self.lon, "alt_m": self.alt_m, "alt_ref": self.alt_ref,
             "geoid_sep_m": self.geoid_sep_m, "stamp_ns": self.stamp_ns,
             "vel_enu": list(self.vel_enu) if self.vel_enu is not None else None,
             "h_acc_m": self.h_acc_m, "v_acc_m": self.v_acc_m}
        return {k: v for k, v in d.items() if v is not None}

    def msl_alt_m(self) -> Optional[float]:
        """Height above mean sea level when it can be known (EXIF's GPSAltitude); else None."""
        if self.alt_m is None:
            return None
        if self.alt_ref == "msl":
            return self.alt_m
        return self.alt_m - self.geoid_sep_m if self.geoid_sep_m is not None else None


@dataclass(frozen=True)
class Orientation:
    yaw_deg: Optional[float] = None     # heading, degrees clockwise from TRUE north
    pitch_deg: Optional[float] = None
    roll_deg: Optional[float] = None

    def to_dict(self) -> dict:
        d = {"yaw_deg": self.yaw_deg, "pitch_deg": self.pitch_deg, "roll_deg": self.roll_deg}
        return {k: v for k, v in d.items() if v is not None}


@dataclass(frozen=True)
class CaptureRequest:
    stamp_ns: Optional[int] = None      # UTC Unix ns of the wanted moment; None = the newest frame
    tolerance_ms: Optional[float] = None
    wait_ms: Optional[float] = None
    gps: Optional[GpsFix] = None
    orientation: Optional[Orientation] = None
    tag: str = "manual"
    id: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"stamp_ns": self.stamp_ns, "tolerance_ms": self.tolerance_ms, "wait_ms": self.wait_ms,
             "tag": self.tag, "id": self.id, "meta": self.meta or None}
        return {k: v for k, v in d.items() if v is not None}


class RequestError(ValueError):
    pass


def _num(obj: dict, key: str, *, required=False, lo=None, hi=None) -> Optional[float]:
    v = obj.get(key)
    if v is None:
        if required:
            raise RequestError(f"missing '{key}'")
        return None
    if isinstance(v, bool):
        raise RequestError(f"'{key}' must be a number")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise RequestError(f"'{key}' must be a number") from None
    if not math.isfinite(x):
        raise RequestError(f"'{key}' must be finite")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise RequestError(f"'{key}' out of range [{lo}, {hi}]: {x}")
    return x


def _stamp(obj: dict, key: str) -> Optional[int]:
    v = obj.get(key)
    if v is None:
        return None
    if isinstance(v, bool):
        raise RequestError(f"'{key}' must be an integer (UTC Unix ns)")
    try:
        n = int(v) if not isinstance(v, float) else int(round(v))
    except (TypeError, ValueError):
        raise RequestError(f"'{key}' must be an integer (UTC Unix ns)") from None
    if n <= 0:
        raise RequestError(f"'{key}' must be > 0")
    return n


def _parse_gps(g) -> GpsFix:
    if not isinstance(g, dict):
        raise RequestError("'gps' must be an object")
    alt_ref = str(g.get("alt_ref", "ellipsoid")).strip().lower()
    if alt_ref not in ALT_REFS:
        raise RequestError(f"'gps.alt_ref' must be one of {ALT_REFS}")
    vel = g.get("vel_enu")
    if vel is not None:
        if not isinstance(vel, (list, tuple)) or len(vel) != 3:
            raise RequestError("'gps.vel_enu' must be [east, north, up] in m/s")
        vel = tuple(_num({"vel_enu": c}, "vel_enu") for c in vel)
    try:
        return GpsFix(
            lat=_num(g, "lat", required=True, lo=-90, hi=90),
            lon=_num(g, "lon", required=True, lo=-180, hi=180),
            alt_m=_num(g, "alt_m"), alt_ref=alt_ref, geoid_sep_m=_num(g, "geoid_sep_m"),
            stamp_ns=_stamp(g, "stamp_ns"), vel_enu=vel,
            h_acc_m=_num(g, "h_acc_m", lo=0), v_acc_m=_num(g, "v_acc_m", lo=0))
    except RequestError as e:
        raise RequestError(f"gps: {e}") from None


def _parse_orientation(o) -> Orientation:
    if not isinstance(o, dict):
        raise RequestError("'orientation' must be an object")
    try:
        return Orientation(yaw_deg=_num(o, "yaw_deg"), pitch_deg=_num(o, "pitch_deg", lo=-90, hi=90),
                           roll_deg=_num(o, "roll_deg", lo=-180, hi=180))
    except RequestError as e:
        raise RequestError(f"orientation: {e}") from None


def _split_parameters(parameters: str) -> dict:
    out = {}
    for part in str(parameters or "").replace("&", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_request(payload: Optional[bytes], parameters: str = "") -> CaptureRequest:
    """A CaptureRequest from a query's payload (JSON object) or, payload-less, its selector
    parameters. Raises RequestError naming the offending field."""
    if payload:
        if len(payload) > MAX_REQUEST_BYTES:
            raise RequestError(f"request too large ({len(payload)} > {MAX_REQUEST_BYTES} bytes)")
        try:
            req = json.loads(bytes(payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise RequestError(f"request is not JSON: {e}") from None
        if not isinstance(req, dict):
            raise RequestError("request must be a JSON object")
    else:
        req = _split_parameters(parameters)
    v = req.get("v", 1)
    if str(v) != "1":
        raise RequestError(f"unsupported request version {v!r} (this plugin speaks v1)")
    meta = req.get("meta") or {}
    if not isinstance(meta, dict):
        raise RequestError("'meta' must be an object")
    tag = req.get("tag", "manual")
    if not isinstance(tag, str) or not tag or len(tag) > 64:
        raise RequestError("'tag' must be a non-empty string (<= 64 chars)")
    rid = req.get("id")
    if rid is not None and not isinstance(rid, (str, int)):
        raise RequestError("'id' must be a string")
    return CaptureRequest(
        stamp_ns=_stamp(req, "stamp_ns"),
        tolerance_ms=_num(req, "tolerance_ms", lo=0, hi=10_000),
        wait_ms=_num(req, "wait_ms", lo=0, hi=10_000),
        gps=_parse_gps(req["gps"]) if req.get("gps") is not None else None,
        orientation=_parse_orientation(req["orientation"]) if req.get("orientation") is not None else None,
        tag=tag, id=str(rid) if rid is not None else None, meta=meta)


def fix_at(gps: GpsFix, frame_utc_ns: int, request_stamp_ns: Optional[int]) -> Tuple[GpsFix, str, Optional[float]]:
    """(fix for the frame, method, dt_ms): the trigger's fix moved to the chosen frame's time when it
    carries a velocity and the gap is small. method: extrapolated | as_given. dt_ms = frame time -
    fix time, or None when the fix's time is unknown (no gps.stamp_ns and no request stamp)."""
    ref = gps.stamp_ns if gps.stamp_ns is not None else request_stamp_ns
    if ref is None:
        return gps, "as_given", None
    dt = (frame_utc_ns - ref) / 1e9
    dt_ms = round(dt * 1e3, 3)
    if gps.vel_enu is None or dt == 0 or abs(dt) > MAX_EXTRAPOLATION_S:
        return gps, "as_given", dt_ms
    ve, vn, vu = gps.vel_enu
    phi = math.radians(gps.lat)
    s2 = math.sin(phi) ** 2
    n_radius = _A / math.sqrt(1 - _E2 * s2)                 # prime vertical
    m_radius = _A * (1 - _E2) / (1 - _E2 * s2) ** 1.5       # meridian
    h = gps.alt_m or 0.0
    lat = gps.lat + math.degrees(vn * dt / (m_radius + h))
    cos_phi = math.cos(phi)
    lon = gps.lon + (math.degrees(ve * dt / ((n_radius + h) * cos_phi)) if abs(cos_phi) > 1e-9 else 0.0)
    lon = (lon + 180.0) % 360.0 - 180.0
    alt = gps.alt_m + vu * dt if gps.alt_m is not None else None
    moved = GpsFix(lat=lat, lon=lon, alt_m=alt, alt_ref=gps.alt_ref, geoid_sep_m=gps.geoid_sep_m,
                   stamp_ns=frame_utc_ns, vel_enu=gps.vel_enu, h_acc_m=gps.h_acc_m, v_acc_m=gps.v_acc_m)
    return moved, "extrapolated", dt_ms
