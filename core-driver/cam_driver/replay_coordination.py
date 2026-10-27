"""Version 1 held-start participant protocol (stdlib only).

Camera bundles vendor this file verbatim. Every session owns a fresh control directory;
atomic command/status replacement and session+plan identities prevent stale releases.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time


class CoordinationError(RuntimeError):
    pass


class CoordinationAborted(CoordinationError):
    pass


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _atomic(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        os.fchmod(fd, 0o644)  # Native Linux host coordinator may use a different UID.
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Participant:
    """Thread-safe participant; callbacks run only in the wait_release caller."""
    TERMINAL = {"completed", "failed", "aborted"}

    @classmethod
    def from_env(cls, suffix, env=None):
        env = os.environ if env is None else env
        keys = ("RIG_REPLAY_CONTROL_DIR", "RIG_REPLAY_SESSION_ID", "RIG_REPLAY_PLAN_ID",
                "RIG_REPLAY_PARTICIPANT")
        if not any(key in env for key in keys):
            return None
        if not all(env.get(key) for key in keys):
            raise CoordinationError("incomplete coordinated replay environment")
        return cls(env[keys[0]], session_id=env[keys[1]], plan_id=env[keys[2]],
                   participant=env[keys[3]] + "." + suffix)

    def __init__(self, directory, *, session_id, plan_id, participant):
        if (not re.fullmatch(r"[0-9a-f]{32}", session_id) or
                not re.fullmatch(r"[0-9a-f]{64}", plan_id) or
                not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z_]+", participant)):
            raise CoordinationError("invalid replay participant identity")
        self.directory = Path(directory)
        self.identity = dict(version=1, session_id=session_id, plan_id=plan_id)
        self.participant = participant
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._state = "preparing"
        self._error = None
        self._seq = -1
        self._command = None
        self._armed = False
        self._committed = False
        self._release = None
        self._held_at = None
        self._ready_at = None
        self._actual = None
        session = self._read("session.json")
        if participant not in session.get("participants", []):
            raise CoordinationError("participant is not registered in replay session")
        self._deadline = session.get("startup_deadline_unix_s")
        if not _number(self._deadline) or self._deadline <= time.time():
            raise CoordinationError("replay startup deadline has expired or is invalid")
        if not (self.directory / "statuses").is_dir():
            raise CoordinationError("replay statuses directory is missing")
        self._write()
        self._thread = threading.Thread(target=self._heartbeat, name="replay-heartbeat", daemon=True)
        self._thread.start()

    def _read(self, name):
        # Docker Desktop bind mounts can briefly report ENOENT during host os.replace.
        # Wait for this exact file, never substitute a cached command or retry bad data.
        until = time.monotonic() + 0.2
        while True:
            try:
                value = json.loads((self.directory / name).read_text())
                break
            except FileNotFoundError as exc:
                if time.monotonic() >= until:
                    raise CoordinationError(f"cannot read replay {name}: {exc}") from exc
                time.sleep(.01)
            except (OSError, ValueError) as exc:
                raise CoordinationError(f"cannot read replay {name}: {exc}") from exc
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in self.identity.items()):
            raise CoordinationError(f"foreign or malformed replay {name}")
        return value

    def _write(self):
        value = dict(self.identity, participant=self.participant, state=self._state,
                     seq=self._seq, updated_at_unix_s=time.time())
        for key, item in (("held_at_unix_s", self._held_at), ("ready_at_unix_s", self._ready_at),
                          ("release_at_unix_s", self._release),
                          ("actual_release_at_unix_s", self._actual), ("error", self._error)):
            if item is not None:
                value[key] = item
        _atomic(self.directory / "statuses" / (self.participant + ".json"), value)

    def _terminal(self, state, error=None):
        with self._lock:
            if self._state in self.TERMINAL:
                return
            self._state, self._error = state, str(error) if error is not None else None
            self._wake.set()
            self._write()

    def failed(self, error):
        self._terminal("failed", error)

    def aborted(self, reason="replay aborted"):
        self._terminal("aborted", reason)

    def completed(self):
        with self._lock:
            if self._actual is None:
                raise CoordinationError("cannot complete before coordinated release")
        self._terminal("completed")

    def started(self, actual_unix=None):
        with self._lock:
            self.poll()
            now = time.time() if actual_unix is None else actual_unix
            if not self._committed or not _number(now) or now < self._release:
                raise CoordinationError("cannot start before committed replay release")
            if self._state == "running":
                return
            self._actual, self._state = now, "running"
            self._write()

    def _heartbeat(self):
        while True:
            try:
                with self._lock:
                    if self._state in self.TERMINAL:
                        return
                    # Observe abort/deadline even while a caller prepares ROS or decoders.
                    command = self._read("command.json")
                    if command.get("op") == "abort":
                        self.aborted("coordinator aborted replay")
                        return
                    if not self._committed and time.time() >= self._deadline:
                        self.aborted("replay startup deadline expired")
                        return
                    self._write()
            except CoordinationError as exc:
                self.failed(exc)
                return
            except Exception as exc:
                self._error = f"replay heartbeat failed: {exc}"
                self._state = "failed"
                self._wake.set()
                return
            self._wake.wait(0.1)
            self._wake.clear()

    def poll(self):
        """Check cancellation/protocol. Does not advance readiness or invoke callbacks."""
        with self._lock:
            if self._state == "aborted":
                raise CoordinationAborted(self._error or "replay aborted")
            if self._state == "failed":
                raise CoordinationError(self._error or "replay failed")
            if self._state == "completed":
                return self._state
            if not self._committed and time.time() >= self._deadline:
                self.aborted("replay startup deadline expired")
                raise CoordinationAborted(self._error)
            command = self._read("command.json")
            if not self._committed and time.time() >= self._deadline:
                self.aborted("replay startup deadline expired")
                raise CoordinationAborted(self._error)
            seq, op = command.get("seq"), command.get("op")
            if type(seq) is not int or seq < 0 or op not in {"hold", "arm", "prepare_release", "commit", "abort"}:
                raise CoordinationError("invalid replay command")
            if seq < self._seq or (seq == self._seq and command != self._command):
                raise CoordinationError("replay command sequence changed or regressed")
            if op == "abort":
                self.aborted("coordinator aborted replay")
                raise CoordinationAborted(self._error)
            if seq > self._seq:
                if op == "hold" and (self._armed or self._release is not None):
                    raise CoordinationError("replay command regressed to hold")
                if op == "arm" and self._release is not None:
                    raise CoordinationError("replay command regressed to arm")
                if op == "prepare_release":
                    release = command.get("release_at_unix_s")
                    if self._state not in {"ready", "release_ack"}:
                        raise CoordinationError("release proposed before participant was ready")
                    if not _number(release) or (self._release is None and release < time.time() + 0.25):
                        raise CoordinationError("replay release proposal has insufficient future margin")
                    if self._release is not None and release != self._release:
                        raise CoordinationError("replay release instant is immutable")
                    self._release, self._state = release, "release_ack"
                if op == "commit":
                    if self._state not in {"release_ack", "running"} or self._release is None:
                        raise CoordinationError("replay committed without release acknowledgement")
                    if command.get("release_at_unix_s") != self._release:
                        raise CoordinationError("replay commit changed the release instant")
                    if not self._committed and time.time() >= self._release:
                        raise CoordinationError("replay commit arrived after the release instant")
                    self._committed = True
                self._seq, self._command = seq, command
            if self._release is not None and not self._committed and time.time() >= self._release:
                raise CoordinationError("replay release instant passed without commit")
            return self._state

    def wait_release(self, prepared_check=None, ready_check=None, on_arm=None, on_tick=None):
        """Prepare while held, arm without output, then wait for the committed instant.

        Callbacks execute outside the status lock, so expensive preparation cannot starve
        cancellation or heartbeats. They must still be bounded to honor startup deadlines.
        """
        try:
            while True:
                with self._lock:
                    check_readiness = self._state in {"ready", "release_ack"} and not self._committed
                if check_readiness and ready_check is not None and not ready_check():
                    raise CoordinationError("participant lost readiness before commit")
                self.poll()
                with self._lock:
                    preparing = self._state == "preparing"
                if preparing and (prepared_check is None or prepared_check()):
                    with self._lock:
                        self.poll()
                        self._state, self._held_at = "held", time.time()
                        self._write()
                with self._lock:
                    op = self._command.get("op") if self._command else None
                    arm = op == "arm" and not self._armed
                    if arm:
                        if self._state != "held":
                            raise CoordinationError("replay armed before preparation completed")
                        self._armed = True
                if arm and on_arm:
                    on_arm()
                with self._lock:
                    checking = self._armed and self._state == "held"
                if checking and (ready_check is None or ready_check()):
                    with self._lock:
                        self.poll()
                        self._state, self._ready_at = "ready", time.time()
                        self._write()
                if on_tick:
                    on_tick()
                with self._lock:
                    self.poll()
                    self._write()
                    if self._committed and time.time() >= self._release:
                        return self._release
                self._wake.wait(0.02)
                self._wake.clear()
        except CoordinationAborted:
            raise
        except Exception as exc:
            self.failed(exc)
            raise
