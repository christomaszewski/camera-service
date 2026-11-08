"""Where a capture goes (docs/CAPTURE.md "Outputs"). Each sink writes the image file(s) first and the
JSON sidecar last, so a reader can treat the sidecar as the "capture complete" marker.

  file   <camera recording dir>/keyframes/<utc_ns>.{jpg,tif,json} -- the camera's own directory in the
         run (resolve_recording_dir, pinned at process start like the core), so the run is
         self-contained and `rig fleet sync` carries it. Written via a temp file + rename.
  zenoh  a put per file on <store_prefix>/<utc_ns>.{jpg,tif,json} (default
         fleet/<vehicle>/store/<run>/<instance>/keyframes), for the vehicle store: a Zenoh storage on
         fleet/<vehicle>/store/<run>/** keeps every put. Nothing stores a put nobody subscribes to,
         so the sink watches whether a storage matches (a publisher's matching status on its prefix)
         and refuses with `store_absent` rather than writing into the void.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_STORE_PREFIX = "fleet/{vehicle}/store/{run}/{instance}/keyframes"
_KEY_FORBIDDEN = set("*$?#")


def run_id_from_recording_dir(recording_dir: str, rig_data_dir: str) -> Optional[str]:
    """The open run the camera's recordings were pinned to (`<root>/runs/<id>/recordings/...`), or
    None for a flat layout / no data root -- the same answer resolve_recording_dir already gave."""
    root = (rig_data_dir or "").strip().rstrip("/")
    if not root or not recording_dir:
        return None
    rel = os.path.relpath(os.path.realpath(recording_dir), os.path.realpath(root))
    parts = rel.split(os.sep)
    if len(parts) >= 2 and parts[0] == "runs" and parts[1] not in ("", ".", ".."):
        return parts[1]
    return None


class FileSink:
    name = "file"

    def __init__(self, directory: str):
        self.directory = directory

    def describe(self) -> dict:
        return {"name": self.name, "dir": self.directory}

    def _atomic(self, path: str, data: bytes) -> None:
        tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def write(self, base: str, files, sidecar: bytes) -> dict:
        """`files` = [(extension, bytes, mime)]; returns {"ok": True, <ext>: path, ..., "json": path}."""
        try:
            os.makedirs(self.directory, exist_ok=True)
            out = {"ok": True}
            for ext, data, _mime in list(files) + [("json", sidecar, "application/json")]:
                path = os.path.join(self.directory, f"{base}.{ext}")
                self._atomic(path, data)
                out[ext] = path
            return out
        except OSError as e:
            log.warning("file sink: write failed: %s", e)
            return {"ok": False, "error": "write_failed", "detail": str(e)}


def store_prefix(template: str, vehicle: str, run: Optional[str], instance: str) -> Optional[str]:
    """The concrete key prefix, or None when it needs a run and there is none."""
    if "{run}" in template and not run:
        return None
    prefix = template.format(vehicle=vehicle, run=run or "", instance=instance).strip("/")
    if not prefix or _KEY_FORBIDDEN & set(prefix) or "//" in prefix:
        raise ValueError(f"store_prefix: {prefix!r} is not a concrete key (no wildcards or empty chunks)")
    return prefix


class ZenohStoreSink:
    name = "zenoh"

    def __init__(self, prefix: Optional[str]):
        self.prefix = prefix              # None: no open run, so nowhere to put
        self._session = None
        self._zenoh = None
        self._probe = None

    def attach(self, session, zenoh_mod) -> None:
        """Called once the zenoh session is up. The probe publisher is never put on; it exists for
        its matching status (a storage on fleet/<vehicle>/store/<run>/** matches the prefix)."""
        self._session, self._zenoh = session, zenoh_mod
        if self.prefix is None:
            return
        try:
            self._probe = session.declare_publisher(self.prefix)
        except Exception as e:   # noqa: BLE001
            log.warning("zenoh sink: could not declare the matching probe on %s: %s", self.prefix, e)
            self._probe = None

    def detach(self) -> None:
        try:
            if self._probe is not None:
                self._probe.undeclare()
        except Exception:   # noqa: BLE001
            pass
        self._probe, self._session = None, None

    def store_state(self) -> str:
        if self.prefix is None:
            return "no_run"
        if self._session is None:
            return "no_session"
        try:
            matched = self._probe.matching_status.matching if self._probe is not None else True
        except Exception:   # noqa: BLE001 -- a binding without matching status: assume present
            matched = True
        return "present" if matched else "absent"

    def describe(self) -> dict:
        return {"name": self.name, "prefix": self.prefix, "store": self.store_state()}

    def write(self, base: str, files, sidecar: bytes) -> dict:
        state = self.store_state()
        if state != "present":
            return {"ok": False, "error": "store_absent" if state == "absent" else state}
        z = self._zenoh
        opts = {}
        if z is not None:
            opts = {"congestion_control": z.CongestionControl.BLOCK, "priority": z.Priority.DATA_LOW}
        try:
            out = {"ok": True}
            for ext, data, mime in list(files) + [("json", sidecar, "application/json")]:
                key = f"{self.prefix}/{base}.{ext}"
                self._session.put(key, data, encoding=(z.Encoding(mime) if z else None), **opts)
                out[ext] = key
            return out
        except Exception as e:   # noqa: BLE001
            log.warning("zenoh sink: put failed: %s", e)
            return {"ok": False, "error": "put_failed", "detail": str(e)}
