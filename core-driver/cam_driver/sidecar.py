"""Threaded sidecar writer: per-frame CSV + a JSON header describing the recording.

The CSV is written from a background thread so the capture/feed callback never
blocks on disk I/O. The JSON header captures everything post-processing needs to
turn (CSV + video file) back into absolute, per-frame timestamps and original
pixels: the absolute base timestamp, the time-base source, pixel format, Bayer
pattern, tick frequency, geometry, and the PTS convention.

Join key: each CSV row carries both the camera ``frame_id`` and the ``pts_ns``
used in the video file. Recorded frames are matched to rows by PTS (monotonic,
preserved by Matroska); ``frame_id`` continuity reveals dropped frames.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import queue
import threading
from dataclasses import asdict, dataclass
from typing import Optional

from .timestamps import FrameStamp

log = logging.getLogger(__name__)

# timestamp_ns = the chosen source's value; chunk_ns / camera_ns / system_ns are
# logged side-by-side every frame so the PTP-vs-arrival comparison is always available.
CSV_FIELDS = ["frame_id", "pts_ns", "timestamp_ns", "source", "chunk_ns", "camera_ns", "system_ns"]
_SENTINEL = None


@dataclass
class SidecarHeader:
    created_unix_s: float
    base_timestamp_ns: int            # absolute ts of the PTS==0 reference frame
    timestamp_source: str             # ptp_chunk | camera | system
    ptp_synced: bool                  # True if the chunk timestamp is PTP-disciplined wall-clock
    pixel_format: str                 # raw Aravis pixel format string (authoritative)
    bayer_pattern: Optional[str]      # e.g. rggb, or None for mono/color
    bits_per_pixel: int
    width: int
    height: int
    tick_frequency_hz: int            # GevTimestampTickFrequency (1e9 under PTP)
    # How recorded frames are arranged: off = the raw mosaic; plain/green_diff/rct = CFA-tiled into 4
    # quadrant sub-planes (+ optional colour transform). Anything but 'off' means playback must un-tile
    # with cam_driver.bayer_tile.untile_cfa(..., mode=cfa_tile_mode, pattern=bayer_pattern) before demosaicing.
    cfa_tile_mode: str = "off"
    pts_convention: str = "pts_ns = timestamp_ns - base_timestamp_ns"
    absolute_time: str = "absolute_ns = pts_ns + base_timestamp_ns (epoch per timestamp_source)"


class SidecarWriter:
    def __init__(self, path_base: str):
        # path_base e.g. /data/recordings/cam  ->  cam.csv + cam.json
        self._csv_path = path_base + ".csv"
        self._json_path = path_base + ".json"
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=20000)
        self._thread = threading.Thread(target=self._run, name="sidecar-writer", daemon=True)
        self._started = False
        self._dropped = 0

    def write_header(self, header: SidecarHeader) -> None:
        os.makedirs(os.path.dirname(self._json_path) or ".", exist_ok=True)
        with open(self._json_path, "w") as f:
            json.dump(asdict(header), f, indent=2)
        log.info("wrote sidecar header %s", self._json_path)

    def write_summary(self, summary: dict) -> None:
        """Merge a final summary (e.g. drop counters) into the JSON sidecar on stop, so the
        recording's metadata attests how faithful the log is to what was received."""
        try:
            data = {}
            if os.path.exists(self._json_path):
                with open(self._json_path) as f:
                    data = json.load(f)
            data["drops"] = summary
            with open(self._json_path, "w") as f:
                json.dump(data, f, indent=2)
            log.info("wrote sidecar drop summary: %s", summary)
        except (OSError, ValueError) as e:
            log.warning("could not write sidecar summary: %s", e)

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._csv_path) or ".", exist_ok=True)
        self._thread.start()
        self._started = True
        log.info("sidecar CSV -> %s", self._csv_path)

    def add(self, stamp: FrameStamp, pts_ns: int) -> None:
        if not self._started:
            return
        row = (stamp.frame_id, pts_ns, stamp.timestamp_ns, stamp.source.value,
               "" if stamp.chunk_ns is None else stamp.chunk_ns, stamp.camera_ns, stamp.system_ns)
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("sidecar queue full; dropped %d CSV row(s) so far", self._dropped)

    def _run(self) -> None:
        with open(self._csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_FIELDS)
            n = 0
            while True:
                row = self._q.get()
                if row is _SENTINEL:
                    break
                w.writerow(row)
                n += 1
                if n % 100 == 0:
                    f.flush()
            f.flush()
        log.info("sidecar CSV writer stopped (%d rows, %d dropped)", n, self._dropped)

    def stop(self) -> None:
        if self._started:
            self._q.put(_SENTINEL)
            self._thread.join(timeout=5)
            self._started = False
