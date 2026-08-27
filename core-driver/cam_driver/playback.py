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
import time
from dataclasses import dataclass, replace
from typing import List, Optional

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
    because their timestamps are already cycle-shifted monotonic."""

    def __init__(self, speed: float = 1.0):
        self.speed = float(speed)
        self._t0_src: Optional[int] = None
        self._t0_wall = 0

    def wait(self, ts_ns: int, cancel=None) -> None:
        """Sleep until ts_ns is due. `cancel` (a threading.Event) aborts the sleep --
        recorded gaps can be seconds long and must not block a shutdown."""
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
