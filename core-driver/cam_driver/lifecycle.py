"""Lifecycle policy: the service-level state machine in front of the pipeline's recording sessions.

The core runs in one of two steady states, named after ROS 2 managed-node lifecycle so a ROS-side
proxy is a mechanical translation:

  inactive  camera streaming, plugin transport + preview live, NO recorder
  active    ... plus an open recording session (its own pipeline / prefix / sidecar)

`activate` / `deactivate` are the transitions; `activating` / `deactivating` are transient and only
ever observed by a get_state() reader on another thread, because every transition runs synchronously
on the GLib main loop (the same loop the pipeline's watchdog, health tick and bus handlers share).

This module is the POLICY: which requests are legal from which state, idempotency (an orchestrator
that re-sends `activate` gets ok:true, noop:true -- not an error), the "recording disabled by config"
refusal, the state descriptor every control surface publishes, and the remembered-state file that
lets a CRASH restart resume the last commanded state while a graceful stop forgets it. The MECHANISM
(building and finalizing a session) is CapturePipeline.activate / deactivate; this class only talks
to it through those three methods, so it stays pure Python -- no GStreamer, no zenoh -- and the state
machine is unit-tested with a stub pipeline.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

INACTIVE = "inactive"
ACTIVATING = "activating"
ACTIVE = "active"
DEACTIVATING = "deactivating"

# transition -> (from, to)
TRANSITIONS = {
    "activate": (INACTIVE, ACTIVE),
    "deactivate": (ACTIVE, INACTIVE),
}

SERVICE = "camera-service"
SCHEMA_VERSION = 1

_RUN_ID_STRIP = re.compile(r"[^A-Za-z0-9_.-]+")
_RUN_ID_MAX = 64


def sanitize_run_id(value) -> Optional[str]:
    """An optional operator label folded into the session prefix: filesystem-safe, bounded, or None."""
    if value is None:
        return None
    s = _RUN_ID_STRIP.sub("-", str(value)).strip("-.")
    return s[:_RUN_ID_MAX] or None


def transitions_from(state: str) -> list:
    return [t for t, (src, _dst) in TRANSITIONS.items() if src == state]


class Lifecycle:
    """State machine + descriptor + remembered state over a pipeline exposing
    activate(run_id) / deactivate() / get_state()."""

    def __init__(self, pipeline, *, recording_enabled: bool, initial_state: str = ACTIVE,
                 state_file: str = "", resume: bool = True, service: str = SERVICE,
                 instance: str = "camera", segment_seconds: int = 0):
        self._pipe = pipeline
        self._recording_enabled = bool(recording_enabled)
        self._initial_state = initial_state if initial_state in (ACTIVE, INACTIVE) else ACTIVE
        self._state_file = state_file
        self._resume = bool(resume) and bool(state_file)
        self.service = service
        self.instance = instance
        self.segment_seconds = segment_seconds
        self.boot_reason: Optional[str] = None
        self.since_unix_s = time.time()
        self.last_error: Optional[str] = None
        self._observers: list = []
        # A session can end WITHOUT a command (its pipeline errored); the pipeline reports that here
        # so the remembered state + every observer follow the real state.
        if hasattr(pipeline, "on_session_ended"):
            pipeline.on_session_ended = self._on_uncommanded_end

    # ---- state -------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._pipe.get_state()["state"]

    def add_observer(self, fn: Callable[[dict], None]) -> None:
        """fn(descriptor) after every transition (commanded or not). Errors are logged, never raised."""
        self._observers.append(fn)

    def _notify(self) -> None:
        d = self.descriptor()
        for fn in self._observers:
            try:
                fn(d)
            except Exception as e:   # noqa: BLE001 -- an observer must never break a transition
                log.warning("lifecycle observer failed: %s", e)

    # ---- boot --------------------------------------------------------------
    def resolve_boot_state(self):
        """(state, reason). reason: derived (recording disabled) | resumed (crash restart) | config."""
        if not self._recording_enabled:
            return INACTIVE, "derived"
        recalled = self.recall()
        if recalled in (ACTIVE, INACTIVE):
            return recalled, "resumed"
        return self._initial_state, "config"

    def boot(self) -> bool:
        """Enter the boot state. Called once the main pipeline is PLAYING and BEFORE capture starts,
        so a boot-active recording session exists before the first frame. Returns False if the boot
        activation failed (the caller treats that as fatal, as an unbuildable recorder is today)."""
        state, reason = self.resolve_boot_state()
        self.boot_reason = reason
        log.info("lifecycle: booting %s (%s)", state, reason)
        if state == ACTIVE:
            r = self.request("activate")
            if not r.get("ok"):
                log.error("lifecycle: boot activation failed: %s", r.get("error"))
                return False
            return True
        self.since_unix_s = time.time()
        self._notify()
        return True

    # ---- transitions -------------------------------------------------------
    def request(self, transition: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        cur = self.state
        if transition not in TRANSITIONS:
            return self._result(False, cur, error=f"unknown transition {transition!r}")
        src, dst = TRANSITIONS[transition]
        if cur == dst:
            return self._result(True, cur, noop=True)
        if cur != src:
            return self._result(False, cur, error=f"cannot {transition} from {cur}")
        if transition == "activate":
            if not self._recording_enabled:
                return self._result(False, cur, error="recording disabled by config")
            r = self._call(self._pipe.activate, run_id=sanitize_run_id(params.get("run_id")))
        else:
            r = self._call(self._pipe.deactivate)
        err = r.get("error") or (r.get("session") or {}).get("error")
        if r.get("ok"):
            self.since_unix_s = time.time()
            self.last_error = err     # a finalize that reported trouble is worth surfacing
            self.remember(self.state)
        else:
            self.last_error = err or f"{transition} failed"
        out = self._result(bool(r.get("ok")), self.state, error=err if (err or not r.get("ok")) else None)
        if r.get("session") is not None:
            out["session"] = r["session"]
        self._notify()
        return out

    @staticmethod
    def _call(fn, **kw) -> dict:
        try:
            r = fn(**kw)
            return r if isinstance(r, dict) else {"ok": bool(r)}
        except Exception as e:   # noqa: BLE001 -- a mechanism failure is a legible refusal, not a crash
            log.exception("lifecycle: %s raised", getattr(fn, "__name__", fn))
            return {"ok": False, "error": f"{getattr(fn, '__name__', 'transition')} failed: {e}"}

    def _result(self, ok: bool, state: str, error: Optional[str] = None, noop: bool = False) -> dict:
        out = {"ok": ok, "state": state, "descriptor": self.descriptor()}
        if error:
            out["error"] = error
        if noop:
            out["noop"] = True
        return out

    def _on_uncommanded_end(self, result: dict) -> None:
        """The pipeline closed a session on its own (a recorder ERROR). The commanded state was
        active, but the honest state is inactive -- remember THAT, so a crash restart doesn't walk
        straight back into the same failure without an operator seeing last_error first."""
        self.last_error = result.get("error") or (result.get("session") or {}).get("error") \
            or "recording session ended on an error"
        self.since_unix_s = time.time()
        self.remember(INACTIVE)
        self._notify()

    # ---- descriptor --------------------------------------------------------
    def descriptor(self) -> dict:
        st = self._pipe.get_state()
        state = st.get("state", INACTIVE)
        d = {
            "schema_version": SCHEMA_VERSION,
            "service": self.service,
            "instance": self.instance,
            "state": state,
            "transitions": transitions_from(state),
            "since_unix_s": self.since_unix_s,
            "boot_reason": self.boot_reason,
            "recording_enabled": self._recording_enabled,
            "health": st.get("health"),
            "last_error": self.last_error,
        }
        sess = st.get("session")
        if sess:
            d["recording"] = {**sess, "encoder": st.get("encoder"), "segment_seconds": self.segment_seconds}
        return d

    # ---- remembered state --------------------------------------------------
    def remember(self, state: str) -> None:
        if not self._resume:
            return
        try:
            os.makedirs(os.path.dirname(self._state_file) or ".", exist_ok=True)
            tmp = self._state_file + ".tmp"
            with open(tmp, "w") as f:
                f.write(state + "\n")
            os.replace(tmp, self._state_file)
        except OSError as e:
            log.warning("lifecycle: could not remember state in %s: %s", self._state_file, e)

    def forget(self) -> None:
        """Graceful stop: the next boot follows the config, not the last command."""
        if not self._resume:
            return
        try:
            os.unlink(self._state_file)
            log.info("lifecycle: forgot remembered state (clean stop)")
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("lifecycle: could not remove %s: %s", self._state_file, e)

    def recall(self) -> Optional[str]:
        if not self._resume:
            return None
        try:
            with open(self._state_file) as f:
                s = f.read().strip().lower()
        except OSError:
            return None
        if s in (ACTIVE, INACTIVE):
            return s
        log.warning("lifecycle: ignoring unreadable remembered state %r in %s", s, self._state_file)
        return None
