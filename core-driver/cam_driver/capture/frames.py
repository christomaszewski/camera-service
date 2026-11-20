"""The recent-frames buffer and nearest-frame selection (docs/CAPTURE.md "Choosing the frame").

Frames are COPIED out of the transport before they are kept: the JP6 shm area is reused by the
producer as soon as a buffer is released, and holding a unixfd buffer pins the producer's memory.
The buffer is bounded twice -- by time (frames RECEIVED in the last `span_s`) and by bytes -- so a
high-resolution camera degrades to a shorter window rather than unbounded memory. The time bound
uses receipt time, not the match time: a looping replay's stamps jump backwards at every wrap, and
a bound on match time would keep the previous lap's frames.

Selection runs on a worker thread and may WAIT: a trigger can arrive before the frame it names has
come through the pipeline. It waits for a frame at or after the requested time, up to `wait_s`, then
takes the nearest frame within the tolerance. All times here are the normalized match times
(Frame.utc_ns, see timebase).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

# Selection outcomes that are not a frame (also the reply's `error` codes).
NO_FRAMES = "no_frames"
TOO_OLD = "too_old"
NO_FRAME_WITHIN_TOLERANCE = "no_frame_within_tolerance"


@dataclass(frozen=True)
class Frame:
    frame_id: int
    stamp_ns: int               # as the core delivered it (the active source's epoch)
    utc_ns: int                 # normalized match time, UTC Unix ns (timebase.TimeBase.normalize)
    basis: str                  # capture | capture_tai | plugin_arrival
    arrival_ns: int             # when this process received it (CLOCK_REALTIME)
    source: Optional[str]       # the timestamp source when the endpoint carries it (JP6 header), else None
    fmt: str                    # GStreamer raw format (GRAY8, GRAY16_LE, I420, RGB, ...)
    width: int
    height: int
    bayer: Optional[str]        # CFA pattern (rggb|grbg|gbrg|bggr) when the plane is a mosaic
    data: bytes

    @property
    def nbytes(self) -> int:
        return len(self.data)


class FrameRing:
    """Thread-safe, time- and byte-bounded buffer of recent frames, oldest first."""

    def __init__(self, span_s: float = 2.0, max_bytes: int = 256 * 1024 * 1024):
        if span_s <= 0:
            raise ValueError(f"buffer_s: must be > 0, got {span_s!r}")
        if max_bytes <= 0:
            raise ValueError(f"buffer_max_mb: must be > 0, got {max_bytes!r}")
        self.span_ns = int(span_s * 1e9)
        self.max_bytes = int(max_bytes)
        self._frames: deque = deque()
        self._bytes = 0
        self._cond = threading.Condition()
        self.received = 0
        self.evicted_for_bytes = 0

    def push(self, frame: Frame) -> None:
        with self._cond:
            self._frames.append(frame)
            self._bytes += frame.nbytes
            self.received += 1
            horizon = frame.arrival_ns - self.span_ns
            while len(self._frames) > 1 and self._frames[0].arrival_ns < horizon:
                self._bytes -= self._frames.popleft().nbytes
            while len(self._frames) > 1 and self._bytes > self.max_bytes:
                self._bytes -= self._frames.popleft().nbytes
                self.evicted_for_bytes += 1
            self._cond.notify_all()

    def stats(self) -> dict:
        with self._cond:
            if not self._frames:
                return {"frames": 0, "bytes": 0, "oldest_ns": None, "newest_ns": None, "span_ms": 0,
                        "basis": None}
            lo = min(f.utc_ns for f in self._frames)
            hi = max(f.utc_ns for f in self._frames)
            return {"frames": len(self._frames), "bytes": self._bytes, "oldest_ns": lo, "newest_ns": hi,
                    "span_ms": round((hi - lo) / 1e6, 1), "basis": self._frames[-1].basis}

    def interval_ns(self, window: int = 30) -> Optional[int]:
        """The typical frame interval (median of recent gaps), or None with fewer than two frames.
        It sets the default match tolerance: one frame interval, so a single dropped frame is
        still matched."""
        with self._cond:
            recent = list(self._frames)[-window:]
        gaps = sorted(b.utc_ns - a.utc_ns for a, b in zip(recent, recent[1:]) if b.utc_ns > a.utc_ns)
        return gaps[len(gaps) // 2] if gaps else None

    def newest(self) -> Optional[Frame]:
        with self._cond:
            return self._frames[-1] if self._frames else None

    def _nearest_locked(self, t_ns: int) -> Optional[Frame]:
        best = None
        for f in self._frames:
            if best is None or abs(f.utc_ns - t_ns) < abs(best.utc_ns - t_ns):
                best = f
        return best

    def select(self, t_ns: Optional[int], tolerance_ns: int, wait_s: float,
               _clock=time.monotonic) -> Tuple[Optional[Frame], Optional[str]]:
        """(frame, None) or (None, reason). `t_ns=None` means the newest frame (a manual snapshot);
        it waits only when the buffer is still empty."""
        deadline = _clock() + max(0.0, wait_s)
        with self._cond:
            while True:
                remaining = deadline - _clock()
                if not self._frames:
                    if remaining <= 0:
                        return None, NO_FRAMES
                    self._cond.wait(remaining)
                    continue
                if t_ns is None:
                    return self._frames[-1], None
                if self._frames[-1].utc_ns >= t_ns or remaining <= 0:
                    break
                self._cond.wait(remaining)   # the requested moment has not come through yet
            if t_ns < min(f.utc_ns for f in self._frames) - tolerance_ns:
                return None, TOO_OLD
            best = self._nearest_locked(t_ns)
            if abs(best.utc_ns - t_ns) > tolerance_ns:
                return None, NO_FRAME_WITHIN_TOLERANCE
            return best, None
