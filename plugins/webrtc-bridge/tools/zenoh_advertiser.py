"""Generic Zenoh stream advertiser — presence + descriptor for ONE media stream, at ONE key.

GENERIC by design: NO WebRTC / GigE knowledge. Give it a key, a descriptor dict, and an optional
connect endpoint; over a SINGLE reused peer-mode session it declares:
  - a Zenoh LIVELINESS TOKEN at the key  -> presence. Appears now; Zenoh auto-withdraws it when this
                                            process/session dies (no heartbeat). MUST therefore run
                                            inside the producer process so a crash/kill == teardown.
  - a Zenoh QUERYABLE at the same key     -> replies the descriptor as application/json.

This is the producer half of the fleet media-discovery convention (docs/DISCOVERY.md). It is kept free
of any media-stack specifics so the next producer (a sensor, the ros2-bridge, ...) can lift it
unchanged and feed it its own descriptor. The cross-producer contract is the documented spec, not this
code.

FAIL-SAFE: every Zenoh interaction is wrapped — a missing binding, an unreachable router, or any error
is logged and discovery is quietly abandoned WITHOUT raising, so the caller's real work (the media
pipeline) is never taken down. Discovery is strictly best-effort.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

log = logging.getLogger("zenoh_advertiser")


class StreamAdvertiser:
    """Advertise one stream's presence + descriptor over Zenoh. Best-effort; never raises on Zenoh error.

    Usage:
        adv = StreamAdvertiser("fleet/veh/media/cam0", descriptor, connect=["tcp/localhost:7447"])
        adv.advertise()   # on "streaming" (e.g. pipeline PLAYING)
        ...
        adv.close()       # on graceful shutdown; a crash skips this and Zenoh withdraws the token.
    """

    def __init__(self, key: str, descriptor: dict,
                 connect: Optional[Sequence[str]] = None, enabled: bool = True):
        self.key = key
        self.descriptor = descriptor
        self._connect = [e for e in (connect or []) if e]      # [] -> scout only
        self._enabled = enabled
        self._payload = json.dumps(descriptor, separators=(",", ":")).encode("utf-8")
        self._zenoh = None
        self._session = None
        self._token = None
        self._queryable = None

    @property
    def active(self) -> bool:
        return self._token is not None

    def advertise(self) -> bool:
        """Open the session, declare presence + descriptor. Idempotent. Returns True iff advertising."""
        if not self._enabled:
            log.info("discovery disabled; not advertising %s", self.key)
            return False
        if self._session is not None:
            return self.active
        try:
            import zenoh
        except Exception as e:                                 # binding missing -> keep streaming
            log.warning("zenoh import failed (%s); discovery off, streaming continues", e)
            return False
        self._zenoh = zenoh
        try:
            conf = zenoh.Config()
            conf.insert_json5("mode", '"peer"')
            if self._connect:
                conf.insert_json5("connect/endpoints", json.dumps(list(self._connect)))
            self._session = zenoh.open(conf)
            # Queryable FIRST, so a consumer that races the token always finds a descriptor to fetch.
            self._queryable = self._session.declare_queryable(self.key, self._on_query)
            self._token = self._session.liveliness().declare_token(self.key)
            log.info("advertising %s  (connect=%s)", self.key, self._connect or "scout")
            return True
        except Exception as e:
            log.warning("zenoh advertise failed (%s); discovery off, streaming continues", e)
            self._safe_close()
            return False

    def _on_query(self, query) -> None:
        try:
            query.reply(self.key, self._payload, encoding=self._zenoh.Encoding.APPLICATION_JSON)
        except Exception as e:
            log.warning("descriptor reply failed for %s: %s", self.key, e)

    def close(self) -> None:
        """Graceful teardown: undeclare the token + queryable, close the session. Best-effort."""
        if self._session is not None:
            log.info("withdrawing %s", self.key)
        self._safe_close()

    def _safe_close(self) -> None:
        for label, obj in (("token", self._token), ("queryable", self._queryable)):
            try:
                if obj is not None:
                    obj.undeclare()
            except Exception as e:
                log.debug("undeclare %s failed: %s", label, e)
        self._token = None
        self._queryable = None
        try:
            if self._session is not None:
                self._session.close()
        except Exception as e:
            log.debug("session close failed: %s", e)
        self._session = None
