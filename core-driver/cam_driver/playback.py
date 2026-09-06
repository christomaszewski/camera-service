"""Shared helpers for the playback sources (replay/pcap): run discovery, sidecar-stamp
reconstruction, and pacing. Pure logic -- no GStreamer -- so it unit-tests on a bare host.

Timeline conventions (both playback sources):
  * `speed` paces delivery against the DATA's own timestamps: frame k is delivered
    (ts_k - ts_0)/speed after frame 0; speed <= 0 = as fast as the pipeline drains.
  * `retime: original` replays the historical stamps verbatim; `retime: wall` shifts the
    whole run onto the wall clock at start (one constant offset -- inter-frame timing and
    provenance relationships are preserved either way).
  * `loop` shifts each replay cycle by (span + one median interval) so timestamps stay
    strictly monotonic across cycles; frame_ids repeat per cycle (honest: it IS the same
    recorded frame again, and downstream gap accounting only counts forward jumps).
"""
from __future__ import annotations

import csv
import glob
import json
import logging
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, List, Optional

from .timestamps import FrameStamp, TimestampSource

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_NS = 33_333_333   # last-resort frame interval (~30 fps) when underivable


@dataclass
class RunInfo:
    """One recorded run, located on disk: `<base>-NNNNN.mkv` parts + `<base>.csv/.json`."""
    base: str                 # <dir>/<prefix-with-stamp>, no extension
    header: dict              # parsed <base>.json (written by SidecarWriter.write_header)
    csv_path: str
    mkv_paths: List[str]

    @property
    def mkv_glob(self) -> str:
        """Glob for splitmuxsrc, matching exactly this run's parts."""
        return f"{self.base}-*.mkv"


def discover_run(path: str, run: str = "") -> RunInfo:
    """Locate a run from `path`: either a run-prefix path (`<dir>/<prefix>` such that
    `<prefix>.json` exists) or a directory -- one run inside is used directly, several
    picks the most recent (prominently logged; `run` pins one). Legible errors otherwise."""
    if not path:
        raise ValueError("replay.path is empty -- point it at a run directory or a run prefix")
    path = path.rstrip("/")
    if os.path.isdir(path):
        if run:
            base = os.path.join(path, run)
            if not os.path.isfile(base + ".json"):
                raise ValueError(
                    f"replay.run {run!r} not found in {path} (no {run}.json); runs present: "
                    f"{', '.join(_run_names(path)) or 'none'}")
        else:
            names = _run_names(path)
            if not names:
                raise ValueError(
                    f"replay.path {path} contains no runs (no <prefix>.json sidecar found)")
            base = os.path.join(path, names[-1])
            if len(names) > 1:
                log.warning("replay: %d runs in %s -- picking the most recent %r "
                            "(set replay.run to pin another: %s)",
                            len(names), path, names[-1], ", ".join(names))
    else:
        base = path[:-5] if path.endswith(".json") else path
        if not os.path.isfile(base + ".json"):
            raise ValueError(
                f"replay.path {path}: not a directory and {base}.json does not exist -- "
                f"point it at a run directory or a run prefix")
    with open(base + ".json") as f:
        header = json.load(f)
    missing = [k for k in ("pixel_format", "width", "height") if not header.get(k)]
    if missing:
        raise ValueError(f"replay: {base}.json is not a run sidecar header "
                         f"(missing {', '.join(missing)})")
    csv_path = base + ".csv"
    if not os.path.isfile(csv_path):
        raise ValueError(f"replay: {csv_path} missing -- a run needs its sidecar CSV to re-stamp")
    # exactly this run's splitmux parts (-NNNNN.mkv): a sibling run whose prefix merely
    # EXTENDS this one (cam-a vs cam-a-night) must not leak its parts in
    part = re.compile(re.escape(base) + r"-\d{5}\.mkv$")
    mkvs = sorted(p for p in glob.glob(glob.escape(base) + "-*.mkv") if part.match(p))
    if not mkvs:
        raise ValueError(f"replay: no {base}-*.mkv parts found -- was recording enabled for this run?")
    return RunInfo(base=base, header=header, csv_path=csv_path, mkv_paths=mkvs)


def _run_names(dirpath: str) -> List[str]:
    """Run prefixes in a directory, oldest -> newest (by the sidecar's mtime). A run is a
    `.json` WITH its sibling `.csv` -- so an unrelated manifest/registry json can't hijack
    newest-run selection."""
    jsons = [p for p in glob.glob(os.path.join(dirpath, "*.json"))
             if os.path.isfile(p[:-5] + ".csv")]
    return [os.path.basename(p)[:-5] for p in sorted(jsons, key=os.path.getmtime)]


def load_stamps(csv_path: str) -> List[FrameStamp]:
    """Reconstruct the per-frame FrameStamps verbatim from a sidecar CSV (row N = frame N
    of the recording; provenance string -> TimestampSource, unknown values -> SYSTEM)."""
    stamps: List[FrameStamp] = []
    bad_sources = 0
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                source = TimestampSource(row["source"])
            except ValueError:
                bad_sources += 1
                source = TimestampSource.SYSTEM
            stamps.append(FrameStamp(
                frame_id=int(row["frame_id"]),
                timestamp_ns=int(row["timestamp_ns"]),
                source=source,
                system_ns=int(row["system_ns"] or 0),
                camera_ns=int(row["camera_ns"] or 0),
                chunk_ns=int(row["chunk_ns"]) if row.get("chunk_ns") else None,
            ))
    if bad_sources:
        log.warning("%s: %d row(s) with an unknown timestamp source -> treated as 'system'",
                    csv_path, bad_sources)
    return stamps


def median_interval_ns(timestamps_ns: List[int]) -> int:
    """Median inter-frame interval; robust to recorded gaps. Falls back to ~30 fps."""
    if len(timestamps_ns) < 2:
        return DEFAULT_INTERVAL_NS
    diffs = [b - a for a, b in zip(timestamps_ns, timestamps_ns[1:]) if b > a]
    return int(statistics.median(diffs)) if diffs else DEFAULT_INTERVAL_NS


def shift_stamp(st: FrameStamp, offset_ns: int) -> FrameStamp:
    """Shift every time field by one constant (retime/loop): internal deltas + provenance
    relationships survive intact."""
    if not offset_ns:
        return st
    return replace(
        st,
        timestamp_ns=st.timestamp_ns + offset_ns,
        system_ns=st.system_ns + offset_ns,
        camera_ns=st.camera_ns + offset_ns,
        chunk_ns=(st.chunk_ns + offset_ns) if st.chunk_ns is not None else None,
    )


class Pacer:
    """Sleep-based pacing against the data's own timeline (see module docstring).
    The baseline is the first wait()ed timestamp; loop cycles keep the SAME baseline
    because their timestamps are already cycle-shifted monotonic.

    The baseline is also what a runtime change RE-ANCHORS: a speed change or a resume
    after a pause rebases (t0_src, t0_wall) onto (last data ts, now), so the new pace
    applies from here on and a pause is never "caught up" in a burst afterwards."""

    def __init__(self, speed: float = 1.0):
        self.speed = float(speed)
        self._t0_src: Optional[int] = None
        self._t0_wall = 0
        self._last_src: Optional[int] = None

    def rebase(self) -> None:
        """Anchor the timeline at (last waited data ts, now). Called on set_speed and on
        resume; harmless before the first wait."""
        if self._last_src is not None:
            self._t0_src, self._t0_wall = self._last_src, time.monotonic_ns()

    def set_speed(self, speed: float) -> None:
        self.speed = float(speed)
        self.rebase()

    def wait(self, ts_ns: int, cancel=None) -> None:
        """Sleep until ts_ns is due. `cancel` (a threading.Event) aborts the sleep --
        recorded gaps can be seconds long and must not block a shutdown."""
        self._last_src = ts_ns
        if self.speed <= 0:
            return
        now = time.monotonic_ns()
        if self._t0_src is None:
            self._t0_src, self._t0_wall = ts_ns, now
            return
        target = self._t0_wall + int((ts_ns - self._t0_src) / self.speed)
        if target > now:
            delay_s = (target - now) / 1e9
            if cancel is not None:
                cancel.wait(delay_s)
            else:
                time.sleep(delay_s)


# ---- runtime playback control (docs/PLAYBACK.md) -------------------------------------------

PLAYING = "playing"
PAUSED = "paused"
FINISHED = "finished"

STOP, RESUME, RESTART = "stop", "resume", "restart"   # what wait_if_paused() returns
RESTART_TAKE_S = 1.0   # how long request("restart") waits for the feeder to take it before replying


class PlaybackState:
    """The POLICY behind a source's playback control: what `control` accepts from which state,
    idempotency, the descriptor every control surface publishes -- and the shared runtime state
    the feeder reads (speed via the pacer, loop, a pending restart, the pause hold).

    Pure Python and thread-safe: `request()` runs on the GLib main loop (the zenoh adapter
    dispatches there, like change_state); the feeder calls `note_frame()` per frame and
    `wait_if_paused()` at its pacing point from the SOURCE thread. The source supplies one hook,
    `on_restart` (pcap: abandon the current cycle; replay: flush-seek to 0) -- the MECHANISM stays
    in the source, the policy here.
    """

    def __init__(self, source_kind: str, *, speed: float = 1.0, loop: bool = False,
                 duration_s: Optional[float] = None, pacer: Optional[Pacer] = None,
                 on_restart: Optional[Callable[[], None]] = None, clock=time.time):
        self.source_kind = source_kind
        self.pacer = pacer or Pacer(speed)
        self.loop = bool(loop)
        self.duration_s = duration_s
        self._on_restart = on_restart
        self._clock = clock
        self._cond = threading.Condition()
        self._state = PLAYING
        self._cycle = 0
        self._frames = 0
        self._position_ns = 0
        self._cycle_base_ns: Optional[int] = None
        self._restart_pending = False
        self.since_unix_s = clock()
        self.last_error: Optional[str] = None
        self._observers: list = []

    # ---- observation ----------------------------------------------------------
    def add_observer(self, fn: Callable[[dict], None]) -> None:
        self._observers.append(fn)

    def _notify(self) -> None:
        d = self.descriptor()
        for fn in self._observers:
            try:
                fn(d)
            except Exception as e:   # noqa: BLE001 -- an observer must never break a request
                log.warning("playback observer failed: %s", e)

    @property
    def state(self) -> str:
        return self._state

    @property
    def speed(self) -> float:
        return self.pacer.speed

    def controls(self) -> list:
        if self._state == FINISHED:
            return []
        first = "pause" if self._state == PLAYING else "resume"
        return [first, "set_speed", "set_loop", "restart"]

    def descriptor(self) -> dict:
        with self._cond:
            return {
                "schema_version": 1,
                "service": "camera-service",
                "instance": None,            # the adapter fills the key's <instance>
                "source": self.source_kind,
                "state": self._state,
                "controls": self.controls(),
                "speed": self.pacer.speed,
                "loop": self.loop,
                "cycle": self._cycle,
                "position_s": round(self._position_ns / 1e9, 3),
                "duration_s": self.duration_s,
                "frames": self._frames,
                "since_unix_s": self.since_unix_s,
                "last_error": self.last_error,
            }

    # ---- the feeder's side (source thread) ---------------------------------------
    def note_frame(self, data_ts_ns: int) -> None:
        """One frame delivered at the data's own timestamp (cycle-shifted or not: the first
        note after a (re)start anchors the cycle, so position is relative to it)."""
        with self._cond:
            if self._cycle_base_ns is None:
                self._cycle_base_ns = data_ts_ns
            self._position_ns = max(0, data_ts_ns - self._cycle_base_ns)
            self._frames += 1

    def mark_cycle(self, cycle: int) -> None:
        """A new cycle began (loop wrap or restart): position/frames start over."""
        with self._cond:
            self._cycle = cycle
            self._frames = 0
            self._position_ns = 0
            self._cycle_base_ns = None

    def take_restart(self) -> bool:
        """Feeder (or a source's restart hook): was a restart requested since the last check?
        CONSUMES it -- and wakes request(), which waits for exactly this to reply "taken"."""
        with self._cond:
            r, self._restart_pending = self._restart_pending, False
            if r:
                self._cond.notify_all()
            return r

    def wait_if_paused(self, cancel=None) -> str:
        """Feeder: block while paused. Returns STOP (cancel set), RESTART (a restart was pending --
        CONSUMED here, the caller now honours it: a verdict that left the flag set made every
        following cycle restart on its first frame, a tight loop delivering nothing) or RESUME
        (carry on). Never blocks when playing."""
        with self._cond:
            while self._state == PAUSED and not (cancel is not None and cancel.is_set()) \
                    and not self._restart_pending:
                self._cond.wait(0.2)
            if cancel is not None and cancel.is_set():
                return STOP
            if self._restart_pending:
                self._restart_pending = False
                self._cond.notify_all()
                return RESTART
            return RESUME

    def mark_finished(self, error: Optional[str] = None) -> None:
        with self._cond:
            if self._state == FINISHED:
                return
            self._state = FINISHED
            self.since_unix_s = self._clock()
            self.last_error = error
            self._cond.notify_all()
        self._notify()

    # ---- the control surface (main loop) --------------------------------------------
    def request(self, op: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        with self._cond:
            cur = self._state
            if op not in ("pause", "resume", "set_speed", "set_loop", "restart"):
                return self._result(False, error=f"unknown op {op!r}")
            if cur == FINISHED:
                return self._result(False, error="playback has finished")
            if op == "pause":
                if cur == PAUSED:
                    return self._result(True, noop=True)
                self._state = PAUSED
            elif op == "resume":
                if cur == PLAYING:
                    return self._result(True, noop=True)
                self._state = PLAYING
                self.pacer.rebase()          # the paused wall time must not be caught up in a burst
                self._cond.notify_all()
            elif op == "set_speed":
                try:
                    speed = float(params.get("speed"))
                except (TypeError, ValueError):
                    return self._result(False, error="'speed' must be a number >= 0")
                if speed < 0:
                    return self._result(False, error="'speed' must be >= 0")
                if speed == self.pacer.speed:
                    return self._result(True, noop=True)
                self.pacer.set_speed(speed)
            elif op == "set_loop":
                loop = params.get("loop")
                if not isinstance(loop, bool):
                    return self._result(False, error="'loop' must be true or false")
                if loop == self.loop:
                    return self._result(True, noop=True)
                self.loop = loop
            elif op == "restart":
                self._restart_pending = True
                self._cond.notify_all()      # a paused feeder must wake to honour it
            self.since_unix_s = self._clock()
            self.last_error = None
        if op == "restart":
            if self._on_restart is not None:
                try:
                    self._on_restart()       # replay: the seek, which takes the flag itself
                except Exception as e:   # noqa: BLE001 -- a mechanism failure is a legible refusal
                    log.exception("playback: restart hook raised")
                    with self._cond:
                        self._restart_pending = False
                        self.last_error = f"restart failed: {e}"
                    return self._result(False, error=self.last_error)
            # Reply once the feeder has TAKEN it (its next pacing point -- one frame interval,
            # sub-second), so "ok" means "restarted", as the contract promises. Bounded: a
            # feeder deep in a multi-second recorded gap takes it when it wakes; the reply is
            # still honest about what happened.
            with self._cond:
                self._cond.wait_for(lambda: not self._restart_pending, timeout=RESTART_TAKE_S)
                taken = not self._restart_pending
        out = self._result(True)
        if op == "restart" and not taken:
            out["pending"] = True
        self._notify()
        return out

    def _result(self, ok: bool, error: Optional[str] = None, noop: bool = False) -> dict:
        out = {"ok": ok, "state": self._state, "descriptor": self.descriptor()}
        if error:
            out["error"] = error
        if noop:
            out["noop"] = True
        return out
