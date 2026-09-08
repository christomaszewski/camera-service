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


def discover_sessions(path: str, run: str = "") -> List[RunInfo]:
    """Every recorded SESSION under `path`, in timeline order: a run directory holds one per
    lifecycle activate (each its own `<prefix>` triple), and a replay plays them back to back
    with the recorded gaps between them. Ordered by the header's `first_timestamp_ns` (sidecar
    mtime for headers that predate it). `run` pins ONE prefix; a prefix path (or its .json)
    names one directly. Legible errors otherwise."""
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
            names = [run]
        else:
            names = _run_names(path)
            if not names:
                raise ValueError(
                    f"replay.path {path} contains no runs (no <prefix>.json sidecar found)")
        infos = [_load_run(os.path.join(path, n)) for n in names]
        infos.sort(key=lambda r: (int(r.header.get("first_timestamp_ns") or 0),
                                  os.path.getmtime(r.base + ".json")))
        return infos
    base = path[:-5] if path.endswith(".json") else path
    if not os.path.isfile(base + ".json"):
        raise ValueError(
            f"replay.path {path}: not a directory and {base}.json does not exist -- "
            f"point it at a run directory or a run prefix")
    return [_load_run(base)]


def discover_run(path: str, run: str = "") -> RunInfo:
    """ONE session from `path` (the pre-session API): a prefix path names it, a directory with
    several picks the most recent (prominently logged; `run` pins one)."""
    infos = discover_sessions(path, run)
    if len(infos) > 1:
        log.warning("replay: %d runs in %s -- picking the most recent %r "
                    "(set replay.run to pin another: %s)", len(infos), path,
                    os.path.basename(infos[-1].base), ", ".join(os.path.basename(i.base) for i in infos))
    return infos[-1]


def _load_run(base: str) -> RunInfo:
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

    def __init__(self, speed: float = 1.0, anchor_src_ns: Optional[int] = None):
        self.speed = float(speed)
        # The data-timeline ZERO the first wait anchors at "now" (playback.epoch_unix_ns, in the
        # stamps' own -- shifted -- domain). None = the first waited timestamp: playback starts on
        # frame 0. Set: frame 0 is released (ts_0 - anchor)/speed after playback starts, so several
        # producers released together sit at the same place on one shared timeline.
        self.anchor_src_ns = anchor_src_ns
        self._t0_src: Optional[int] = None
        self._t0_wall = 0
        self._last_src: Optional[int] = None

    def reset(self, anchor_src_ns: Optional[int] = None) -> None:
        """Forget the baseline: the next wait() anchors afresh at now (a restart from the top --
        with or without a hold in between -- must not pay for the wall time the old baseline
        says has elapsed, nor burst through it)."""
        self.anchor_src_ns = anchor_src_ns
        self._t0_src = None
        self._last_src = None

    def anchor_now(self) -> None:
        """Start the timeline NOW at the configured zero (a release from the boot hold): the
        first wait then paces against this instant instead of anchoring itself, so a silent
        lead-in is measured from the release, not from the first frame's arrival."""
        if self.anchor_src_ns is not None:
            self._t0_src, self._t0_wall = self.anchor_src_ns, time.monotonic_ns()

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
            self._t0_src = self.anchor_src_ns if self.anchor_src_ns is not None else ts_ns
            self._t0_wall = now
            if self.anchor_src_ns is None:
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
                 on_restart: Optional[Callable[[], None]] = None, clock=time.time,
                 initial_state: str = PLAYING, start_at_unix_s: Optional[float] = None,
                 restartable_when_finished: bool = False):
        self.source_kind = source_kind
        self.pacer = pacer or Pacer(speed)
        self.loop = bool(loop)
        self.duration_s = duration_s
        self._on_restart = on_restart
        self._clock = clock
        self._cond = threading.Condition()
        self._state = PAUSED if initial_state == PAUSED else PLAYING
        self._cycle = 0
        self._frames = 0
        self._position_ns = 0
        self._cycle_base_ns: Optional[int] = None
        self._restart_pending = False
        # A source that can start over after its end (replay rebuilds its reader) advertises
        # `restart` while finished; one that cannot (pcap's feeder has exited) advertises nothing.
        self._restartable = bool(restartable_when_finished)
        # "paused" at boot means HOLD ON THE FIRST FRAME: consumers (the bridges, a viewer) get one
        # frame to negotiate and show, then nothing until release. The feeder takes the preroll
        # once, for the very first frame, and holds from the second on.
        self._preroll_pending = self._state == PAUSED
        self.since_unix_s = clock()
        self.last_error: Optional[str] = None
        self._observers: list = []
        # Timeline fields the source fills for the descriptor (docs/PLAYBACK.md).
        self.source_path: Optional[str] = None    # what is being played right now (a session prefix)
        self.session: Optional[int] = None        # 0-based index of that session in the run
        self.sessions: Optional[int] = None       # how many the run holds
        self.epoch_unix_ns: Optional[int] = None  # the timeline zero (position_s counts from it)
        # The release gate: an orchestrator starts N producers paused and hands them all the same
        # instant; each resumes itself there, so a replay's cameras and bag player start together.
        self._gate: Optional[threading.Timer] = None
        self._gate_armed = False
        if start_at_unix_s is not None:
            self.arm_start_gate(float(start_at_unix_s))

    def arm_start_gate(self, at_unix_s: float) -> None:
        """Resume at the wall instant `at_unix_s` (a no-op if not paused by then, or if an
        operator touched playback first -- their intent wins over a schedule)."""
        delay = at_unix_s - self._clock()
        if self._state != PAUSED:
            log.warning("playback: start gate %.3f ignored -- playback is not paused", at_unix_s)
            return
        if delay <= 0:
            log.warning("playback: start gate %.3f is %.1fs in the past -- releasing now",
                        at_unix_s, -delay)
            with self._cond:
                self._release()
            return
        self._gate_armed = True
        self._gate = threading.Timer(delay, self._fire_gate)
        self._gate.daemon = True
        self._gate.start()
        log.info("playback: paused; start gate in %.1fs (%.3f)", delay, at_unix_s)

    def _release(self) -> None:
        """Leave the boot hold (under the lock): playing, and the timeline starts NOW at the
        configured zero -- frame 0 sits (ts_0 - epoch)/speed after this instant, not at it, and
        position moves from 0 through any silent lead-in."""
        self._state = PLAYING
        self.since_unix_s = self._clock()
        self.pacer.reset(self.pacer.anchor_src_ns)
        self.pacer.anchor_now()

    def _fire_gate(self) -> None:
        if not self._gate_armed:
            return
        self._gate_armed = False
        if self._state == PAUSED:
            log.info("playback: start gate reached -> resume")
            self.request("resume")

    def cancel_start_gate(self) -> None:
        self._gate_armed = False
        if self._gate is not None:
            self._gate.cancel()
            self._gate = None

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
            return ["restart"] if self._restartable else []
        first = "pause" if self._state == PLAYING else "resume"
        return [first, "set_speed", "set_loop", "restart"]

    def _timeline_position_ns(self) -> int:
        """Position on the timeline: the last delivered frame's -- or, while PLAYING through
        silence (the lead-in before a session that started minutes into the run, a gap between
        sessions), where the timeline itself has got to: the pacer's anchor advanced by the wall
        time since it, at `speed`. A viewer's scrubber moves through silence instead of sitting at
        the last frame; never past the duration."""
        pos = self._position_ns
        p = self.pacer
        if self._state == PLAYING and p._t0_src is not None and p.speed > 0:
            # before the first delivered frame the cycle's base IS the anchor (the zero)
            base = self._cycle_base_ns if self._cycle_base_ns is not None else p._t0_src
            wall = (time.monotonic_ns() - p._t0_wall) * p.speed
            timeline = int(wall) + (p._t0_src - base)
            pos = max(pos, timeline)
            if self.duration_s is not None:
                pos = min(pos, int(self.duration_s * 1e9))
        return max(0, pos)

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
                "position_s": round(self._timeline_position_ns() / 1e9, 3),
                "duration_s": self.duration_s,
                "frames": self._frames,
                "since_unix_s": self.since_unix_s,
                "last_error": self.last_error,
                "source_path": self.source_path,
                "session": self.session,
                "sessions": self.sessions,
                "epoch_unix_ns": self.epoch_unix_ns,
            }

    # ---- the feeder's side (source thread) ---------------------------------------
    def note_frame(self, data_ts_ns: int, cycle_base_ns: Optional[int] = None) -> None:
        """One frame delivered at the data's own timestamp (cycle-shifted or not). Position is
        relative to the cycle's base: `cycle_base_ns` when the source has a timeline zero (the
        epoch, shifted like the stamps -- and moved forward by whatever silence it skipped, so
        position stays EFFECTIVE playback time), else the first frame noted after a (re)start."""
        with self._cond:
            if cycle_base_ns is not None:
                self._cycle_base_ns = cycle_base_ns
            elif self._cycle_base_ns is None:
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

    def take_preroll(self) -> bool:
        """Feeder: deliver this frame WITHOUT the pause hold? True exactly once -- for the first
        frame of a playback that boots paused -- so a held source still shows its first frame."""
        with self._cond:
            r, self._preroll_pending = self._preroll_pending, False
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
        self._gate_armed = False      # an operator's request outranks a scheduled release
        with self._cond:
            cur = self._state
            if op not in ("pause", "resume", "set_speed", "set_loop", "restart"):
                return self._result(False, error=f"unknown op {op!r}")
            if cur == FINISHED:
                if not (op == "restart" and self._restartable):
                    return self._result(False, error="playback has finished")
                self._state = PLAYING     # a restart from the end plays from the top
            if op == "pause":
                if cur == PAUSED:
                    return self._result(True, noop=True)
                self._state = PAUSED
            elif op == "resume":
                if cur == PLAYING:
                    return self._result(True, noop=True)
                if self._frames == 0 and self.pacer.anchor_src_ns is not None:
                    self._release()          # from the boot hold: the timeline starts NOW at its zero
                else:
                    self._state = PLAYING
                    self.pacer.rebase()      # the paused wall time must not be caught up in a burst
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
