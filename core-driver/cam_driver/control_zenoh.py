"""The zenoh control plane: presence, state and `change_state` for the lifecycle, at

    fleet/<vehicle_id>/svc/<instance>/lifecycle               liveliness token + queryable (the descriptor)
    fleet/<vehicle_id>/svc/<instance>/lifecycle/change_state  queryable: {"transition": ..., "run_id"?: ...}
    fleet/<vehicle_id>/svc/<instance>/lifecycle/state         publisher: the descriptor on every transition

-- the producer half of the service-lifecycle convention (docs/LIFECYCLE.md), shaped after the media
discovery advertiser (plugins/webrtc-bridge/tools/zenoh_advertiser.py, docs/DISCOVERY.md): ONE
peer-mode session, a liveliness token that Zenoh withdraws by itself when this process dies (no
heartbeat -- which is why this lives INSIDE the core), and the same fail-safe discipline: every zenoh
interaction is wrapped, a missing binding or an unreachable router is logged and the control plane is
quietly abandoned (and retried), never the capture path.

Threading: zenoh delivers queries on its own threads. A `change_state` query is marshalled onto the
GLib main loop (the only thread the pipeline's session hooks are safe on) and answered from there,
holding the Query object until the reply is sent -- a query finalizes when it is dropped. The reply
goes out when the transition COMPLETES (activate: sub-second; deactivate: files finalized, bounded by
the session drain), so a caller's ack means the files are closed. The descriptor query is answered on
the zenoh thread directly: it only reads fresh scalars.

Kept binding-free and gi-free at import so the request/reply logic is unit-tested in CI without either
(the session factory and the loop dispatcher are injectable).
"""
from __future__ import annotations

import json
import logging
import os
import socket
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_CONNECT = "tcp/localhost:7447"
RETRY_S = 5
# Stock zenoh peers wait FOREVER for every listed endpoint (connect/timeout_ms = -1), so without this
# open() would block the core until a router appears; with it the endpoints keep retrying in the
# background (1 s -> 4 s backoff) and the pipeline starts regardless.
CONNECT_TIMEOUT_MS = 2000


# ---- naming ------------------------------------------------------------------
def vehicle_id() -> str:
    return os.environ.get("VEHICLE_ID") or socket.gethostname()


def lifecycle_key(vehicle: str, instance: str) -> str:
    return f"fleet/{vehicle}/svc/{instance}/lifecycle"


def zenoh_connect_endpoints(configured: Optional[str]) -> list:
    """The endpoints to connect to: the YAML value when set, else the ZENOH_CONNECT env, else the
    vehicle's local zenohd. Comma-separated; an EMPTY string means scout only (multicast) -- the same
    convention as the bridge's ZENOH_CONNECT."""
    raw = configured if configured is not None else os.environ.get("ZENOH_CONNECT", DEFAULT_CONNECT)
    return [e.strip() for e in str(raw).split(",") if e.strip()]


# ---- wire helpers (pure) -----------------------------------------------------
def encode_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")


def _split_parameters(parameters: str) -> dict:
    out = {}
    for part in str(parameters or "").replace("&", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_change_state(payload: Optional[bytes], parameters: str = ""):
    """(request, error) from a change_state query: a JSON object payload {"transition": "activate",
    "run_id"?: "..."} -- or, for a payload-less get, the selector parameters
    (`...?transition=activate;run_id=leg3`). Unknown keys are ignored."""
    req = None
    if payload:
        try:
            req = json.loads(bytes(payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return None, f"request is not JSON: {e}"
        if not isinstance(req, dict):
            return None, "request must be a JSON object"
    else:
        req = _split_parameters(parameters)
    transition = req.get("transition")
    if not isinstance(transition, str) or not transition:
        return None, "missing 'transition'"
    out = {"transition": transition}
    run_id = req.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, (str, int)):
            return None, "'run_id' must be a string"
        out["run_id"] = str(run_id)
    return out, None


# ---- the adapter ---------------------------------------------------------------
class ZenohControl:
    """Advertise + serve one service instance's lifecycle over zenoh. Best-effort; never raises."""

    def __init__(self, lifecycle, key_base: str, connect: Optional[Sequence[str]] = None,
                 enabled: bool = True, dispatch: Optional[Callable] = None,
                 session_factory: Optional[Callable] = None):
        self.lifecycle = lifecycle
        self.key_base = key_base
        self._connect = [e for e in (connect or []) if e]        # [] -> scout only
        self._enabled = enabled
        self._dispatch = dispatch or _glib_dispatch                # (fn, *args) -> run fn on the main loop
        self._open = session_factory or self._open_zenoh
        self._zenoh = None
        self._session = None
        self._token = None
        self._queryables = []
        self._publisher = None
        self._closed = False
        lifecycle.add_observer(self._on_transition)

    @property
    def active(self) -> bool:
        return self._token is not None

    # ---- lifecycle of the control plane itself ---------------------------
    def start(self, retry_s: int = RETRY_S) -> None:
        """Advertise now; if zenoh isn't reachable yet (a router that comes up after the core -- a
        cold rack boot brings everything up together), retry on a timer until the token lands."""
        if self.advertise():
            return
        try:
            from gi.repository import GLib
        except Exception:   # noqa: BLE001 -- no loop to retry on (tests); stay off
            return
        log.info("control plane: zenoh not reachable yet; retrying every %ds", retry_s)
        GLib.timeout_add_seconds(retry_s, self._retry)

    def _retry(self) -> bool:
        if self._closed or self.advertise():
            if self.active:
                log.info("control plane: presence declared on retry; %s is now controllable", self.key_base)
            return False   # stop the timer
        return True        # keep retrying

    def advertise(self) -> bool:
        """Open the session and declare everything. Idempotent. Returns True iff advertising."""
        if not self._enabled:
            log.info("control plane disabled; lifecycle via signals only")
            return False
        if self._session is not None:
            return self.active
        try:
            self._session = self._open(self._connect)
            # Queryables and the publisher FIRST, the token LAST: a consumer that reacts to the token
            # must always find something to query.
            self._queryables.append(self._session.declare_queryable(self.key_base, self._on_query_state))
            self._queryables.append(
                self._session.declare_queryable(self.key_base + "/change_state", self._on_query_change))
            self._publisher = self._session.declare_publisher(self.key_base + "/state")
            self._token = self._session.liveliness().declare_token(self.key_base)
            log.info("control plane: %s  (connect=%s)", self.key_base, self._connect or "scout")
            self._publish(self.lifecycle.descriptor())
            return True
        except Exception as e:   # noqa: BLE001 -- the control plane must never take capture down
            log.warning("control plane: zenoh advertise failed (%s); capture continues", e)
            self._safe_close()
            return False

    def _open_zenoh(self, connect):
        import zenoh   # lazy: a missing binding degrades to "no control plane", it never crashes the core
        self._zenoh = zenoh
        try:
            zenoh.init_log_from_env_or("error")   # the background connector would otherwise log every retry
        except Exception:   # noqa: BLE001 -- older binding
            pass
        conf = zenoh.Config()
        conf.insert_json5("mode", '"peer"')
        if connect:
            conf.insert_json5("connect/endpoints", json.dumps(list(connect)))
        conf.insert_json5("connect/timeout_ms", str(CONNECT_TIMEOUT_MS))
        conf.insert_json5("connect/exit_on_failure", "false")
        return zenoh.open(conf)

    def close(self) -> None:
        """Graceful teardown: withdraw presence, undeclare, close. Crash/kill skips this and Zenoh
        withdraws the token itself."""
        self._closed = True
        if self._session is not None:
            log.info("control plane: withdrawing %s", self.key_base)
        self._safe_close()

    def _safe_close(self) -> None:
        for label, obj in [("token", self._token), ("publisher", self._publisher)] + \
                [("queryable", q) for q in self._queryables]:
            try:
                if obj is not None:
                    obj.undeclare()
            except Exception as e:   # noqa: BLE001
                log.debug("undeclare %s failed: %s", label, e)
        self._token = None
        self._publisher = None
        self._queryables = []
        try:
            if self._session is not None:
                self._session.close()
        except Exception as e:   # noqa: BLE001
            log.debug("session close failed: %s", e)
        self._session = None

    # ---- queries -----------------------------------------------------------
    def _json_encoding(self):
        return self._zenoh.Encoding.APPLICATION_JSON if self._zenoh is not None else None

    def _reply(self, query, obj) -> None:
        try:
            query.reply(self.key_base if not hasattr(query, "key_expr") else query.key_expr,
                        encode_json(obj), encoding=self._json_encoding())
        except Exception as e:   # noqa: BLE001
            log.warning("control plane: reply failed: %s", e)

    def _on_query_state(self, query) -> None:
        """get_state: answered on the zenoh thread -- the descriptor is fresh scalars only."""
        try:
            self._reply(query, self.lifecycle.descriptor())
        except Exception as e:   # noqa: BLE001
            log.warning("control plane: descriptor reply failed: %s", e)

    def _on_query_change(self, query) -> None:
        """change_state: hand the query to the main loop, where the transition is safe to run."""
        try:
            self._dispatch(self._handle_change, query)
        except Exception as e:   # noqa: BLE001
            log.warning("control plane: could not dispatch change_state: %s", e)
            self._reply(query, {"ok": False, "error": f"dispatch failed: {e}"})

    def _handle_change(self, query) -> bool:
        """Main loop. Parse, run the transition to completion, reply. Always returns False (one-shot)."""
        try:
            payload = getattr(query, "payload", None)
            raw = None
            if payload is not None:
                raw = payload.to_bytes() if hasattr(payload, "to_bytes") else bytes(payload)
            params = getattr(query, "parameters", "")
            req, err = parse_change_state(raw, str(params) if params is not None else "")
            if err:
                result = {"ok": False, "error": err, "state": self.lifecycle.state,
                          "descriptor": self.lifecycle.descriptor()}
            else:
                result = self.lifecycle.request(req["transition"], req)
                log.info("control plane: %s -> ok=%s state=%s%s", req["transition"], result.get("ok"),
                         result.get("state"), f" error={result['error']}" if result.get("error") else "")
            self._reply(query, result)
        except Exception as e:   # noqa: BLE001
            log.exception("control plane: change_state failed")
            self._reply(query, {"ok": False, "error": f"internal error: {e}"})
        return False

    # ---- state publication ---------------------------------------------------
    def _on_transition(self, descriptor: dict) -> None:
        self._publish(descriptor)

    def _publish(self, descriptor: dict) -> None:
        if self._publisher is None:
            return
        try:
            self._publisher.put(encode_json(descriptor), encoding=self._json_encoding())
        except Exception as e:   # noqa: BLE001
            log.warning("control plane: state publish failed: %s", e)


def _glib_dispatch(fn, *args):
    from gi.repository import GLib
    GLib.idle_add(fn, *args)
