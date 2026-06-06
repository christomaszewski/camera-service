"""Per-frame timestamp extraction with a PTP-first fallback ladder.

The hardware (PTP) timestamp parsed from the GVSP chunk data is the *primary*
source. If PTP is unavailable we fall back, in order, to:
  1. the camera-clock timestamp Aravis derives from the GVSP leader packet, then
  2. the host arrival time (the interim "system time on arrival" fallback).

The active source is resolved once at start-up so a recording has a single,
consistent time base, but every frame still records which source actually
produced its stamp -- so post-processing always knows the provenance, and a
mid-stream degrade (e.g. a frame missing its chunk) is visible rather than silent.

Aravis note: arv_buffer_get_timestamp() (camera clock) and
arv_buffer_get_system_timestamp() (host arrival) already return nanoseconds --
there are no _ns variants. ChunkTimestamp is read via the ArvChunkParser by its
feature *node* name (with the 'Chunk' prefix).
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Optional

try:  # gi may be absent on a dev laptop; keep this module importable for unit tests
    from gi.repository import GLib
    _GLibError = GLib.Error
except (ImportError, ValueError):
    class _GLibError(Exception):
        """Fallback when PyGObject isn't installed (Aravis errors are GLib.Error at runtime)."""

log = logging.getLogger(__name__)


class TimestampSource(enum.Enum):
    PTP_CHUNK = "ptp_chunk"   # GigE: PTP-synced ChunkTimestamp (primary)
    CAMERA = "camera"         # GigE: Aravis buffer camera-clock timestamp
    SOF = "sof"               # USB/v4l2: kernel start-of-frame (CLOCK_MONOTONIC, pre-transfer) -- on HW
    RTP_NTP = "rtp_ntp"       # RTSP: camera wall-clock from RTCP SRs (gst>=1.24 reference-timestamp-meta)
    SYSTEM = "system"         # host arrival time (universal fallback)


@dataclass
class FrameStamp:
    frame_id: int
    timestamp_ns: int          # the chosen absolute timestamp, in the active source's epoch
    source: TimestampSource    # which source actually produced timestamp_ns
    system_ns: int             # host arrival time (CLOCK_REALTIME)
    camera_ns: int             # GVSP leader-packet timestamp (PTP when locked)
    chunk_ns: Optional[int] = None   # ChunkTimestamp tick-converted to ns (None if no chunk)


class TimestampExtractor:
    """Reads (frame_id, timestamp) off a live, SUCCESS-status Aravis buffer."""

    def __init__(
        self,
        chunk_parser=None,
        chunk_timestamp_name: str = "ChunkTimestamp",
        chunk_frame_id_name: str = "ChunkFrameID",
        prefer: TimestampSource = TimestampSource.PTP_CHUNK,
        tick_frequency_hz: int = 0,
    ):
        self._parser = chunk_parser
        self._chunk_ts = chunk_timestamp_name
        self._chunk_fid = chunk_frame_id_name
        self._prefer = prefer
        self._tick_hz = tick_frequency_hz
        self._active: Optional[TimestampSource] = None
        self._ptp_locked = False

    def set_chunk_parser(self, parser, tick_frequency_hz: Optional[int] = None) -> None:
        """Swap in a fresh chunk parser after a camera reconnect (the old parser belonged
        to the disconnected device). Keeps the already-resolved active source."""
        self._parser = parser
        if tick_frequency_hz is not None:
            self._tick_hz = tick_frequency_hz

    @property
    def ptp_locked(self) -> bool:
        return self._ptp_locked

    @property
    def active_source(self) -> TimestampSource:
        return self._active or TimestampSource.SYSTEM

    def resolve_active_source(self, ptp_locked: bool, chunks_enabled: bool) -> TimestampSource:
        """Pick the time base once. The chunk timestamp is used whenever chunk data is
        available -- ptp_locked is recorded as provenance (whether that timestamp is
        PTP-disciplined wall-clock, or a free-running camera counter)."""
        self._ptp_locked = ptp_locked
        ladder = {
            TimestampSource.PTP_CHUNK: (
                TimestampSource.PTP_CHUNK, TimestampSource.CAMERA, TimestampSource.SYSTEM),
            TimestampSource.CAMERA: (TimestampSource.CAMERA, TimestampSource.SYSTEM),
            TimestampSource.SYSTEM: (TimestampSource.SYSTEM,),
        }[self._prefer]

        chosen = TimestampSource.SYSTEM
        for cand in ladder:
            if cand is TimestampSource.PTP_CHUNK and not (chunks_enabled and self._parser):
                continue
            chosen = cand
            break

        self._active = chosen
        log.info("Active timestamp source = %s (prefer=%s, chunks=%s, ptp_locked=%s)",
                 chosen.value, self._prefer.value, chunks_enabled, ptp_locked)
        if chosen is TimestampSource.PTP_CHUNK and not ptp_locked:
            log.warning("Using chunk timestamps but PTP is NOT locked -- they're a free-running "
                        "camera clock, not wall-clock (ptp_synced=false in the sidecar).")
        elif self._prefer is TimestampSource.PTP_CHUNK and chosen is not TimestampSource.PTP_CHUNK:
            log.warning("Chunk timestamps unavailable; using %s.", chosen.value)
        return chosen

    def extract(self, buf) -> FrameStamp:
        # Read every candidate up front so the sidecar can log all three for the
        # PTP-vs-arrival experiment (docs/ptp-timestamp-experiment.md), then select
        # per the active source with a per-frame degrade.
        system_ns = int(buf.get_system_timestamp())   # host arrival (CLOCK_REALTIME)
        camera_ns = int(buf.get_timestamp())           # GVSP leader-packet clock; ns; PTP when locked
        gvsp_fid = int(buf.get_frame_id())

        chunk_ns: Optional[int] = None
        chunk_fid: Optional[int] = None
        if self._parser is not None:
            raw = self._read_chunk_int(self._chunk_ts, buf)
            chunk_ns = self._ticks_to_ns(raw) if raw is not None else None
            chunk_fid = self._read_chunk_int(self._chunk_fid, buf)

        frame_id = chunk_fid if chunk_fid is not None else gvsp_fid  # chunk id authoritative when present
        active = self._active or TimestampSource.SYSTEM

        if active is TimestampSource.PTP_CHUNK and chunk_ns is not None:
            ts, used = chunk_ns, TimestampSource.PTP_CHUNK
        elif active in (TimestampSource.PTP_CHUNK, TimestampSource.CAMERA) and camera_ns > 0:
            ts, used = camera_ns, TimestampSource.CAMERA      # primary degrade, or CAMERA preference
        else:
            ts, used = system_ns, TimestampSource.SYSTEM

        return FrameStamp(
            frame_id=frame_id, timestamp_ns=int(ts), source=used,
            system_ns=system_ns, camera_ns=camera_ns, chunk_ns=chunk_ns,
        )

    def _ticks_to_ns(self, raw: int) -> int:
        # ChunkTimestamp is a raw device-tick count. Under PTP the tick frequency is
        # 1 GHz so ticks == ns; otherwise convert. (arv_buffer_get_timestamp() is
        # already ns -- Aravis converts it -- so only the raw chunk value needs this.)
        if self._tick_hz and self._tick_hz != 1_000_000_000:
            return raw * 1_000_000_000 // self._tick_hz
        return raw

    def _read_chunk_int(self, name: str, buf=None) -> Optional[int]:
        if not self._parser or buf is None:
            return None
        try:
            if hasattr(buf, "has_chunks") and not buf.has_chunks():
                return None
            return int(self._parser.get_integer_value(buf, name))
        except _GLibError as e:
            log.debug("chunk read of %s failed: %s", name, e)
            return None
