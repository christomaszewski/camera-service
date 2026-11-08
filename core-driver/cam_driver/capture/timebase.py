"""Which clock a buffered frame is matched on (docs/CAPTURE.md "Time").

A trigger names a moment in UTC Unix nanoseconds (ROS time). The core hands the plugin each frame's
timestamp "in the active source's epoch" (cam_driver.timestamps), which is one of:
  * UTC already -- `system` (host arrival), `sof` (USB, lifted to CLOCK_REALTIME), `rtp_ntp` (RTSP);
  * PTP time -- `ptp_chunk`, or `camera` while PTP is locked. PTP normally runs on TAI, which is
    UTC + the leap-second offset (37 s since 2017), unless the grandmaster serves UTC;
  * a free-running camera counter -- `ptp_chunk` / `camera` WITHOUT a PTP lock, typically counting
    from power-on. Not on any absolute clock.

The JP7 unixfd endpoint does not say which source a stamp came from, and neither endpoint says
whether PTP is locked, so the decision is made per frame by PLAUSIBILITY against the plugin's own
receipt time: a capture time must precede receipt by no more than a latency budget. A UTC stamp
passes as-is, a TAI stamp passes once the offset is removed, and a counter (decades off) passes
neither -- that frame is matched on its receipt time instead (`plugin_arrival`), a few ms after the
core's own arrival stamp. Playback sources deliver RECORDED capture times, which are never close to
now, so `auto` trusts their stamps as delivered.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Tuple

TAI_UTC_DEFAULT_S = 37              # TAI - UTC since 2017-01-01; the kernel's value wins when set

BASES = ("auto", "capture", "arrival")
SCALES = ("auto", "utc", "tai")

# What the per-frame decision reports (replies, sidecars, the descriptor):
CAPTURE = "capture"                 # the core's stamp, already UTC
CAPTURE_TAI = "capture_tai"         # the core's stamp, TAI with the offset removed
PLUGIN_ARRIVAL = "plugin_arrival"   # this process's receipt time (the stamp was not wall-clock)


def kernel_tai_offset_s() -> int:
    """CLOCK_TAI - CLOCK_REALTIME in whole seconds; 0 when the kernel was never told (ptp4l /
    phc2sys / chrony set it on a synced host) or the clock is unavailable."""
    try:
        return int(round((time.clock_gettime_ns(time.CLOCK_TAI) - time.time_ns()) / 1e9))
    except (AttributeError, OSError):
        return 0


def resolve_tai_offset_s(configured) -> int:
    """`auto` -> the kernel's offset when it is set, else TAI_UTC_DEFAULT_S; a number is used as-is."""
    if configured in (None, "", "auto"):
        return kernel_tai_offset_s() or TAI_UTC_DEFAULT_S
    return int(configured)


@dataclass(frozen=True)
class TimeBase:
    basis: str = "auto"             # auto | capture | arrival
    scale: str = "auto"             # auto | utc | tai -- what the capture stamps are
    tai_offset_ns: int = TAI_UTC_DEFAULT_S * 1_000_000_000
    max_age_ns: int = 5_000_000_000      # a capture may precede receipt by at most this
    max_ahead_ns: int = 500_000_000      # clock disagreement tolerated the other way
    playback: bool = False          # a replay/pcap source: `auto` trusts the recorded stamps

    def __post_init__(self):
        if self.basis not in BASES:
            raise ValueError(f"time_basis: expected one of {BASES}, got {self.basis!r}")
        if self.scale not in SCALES:
            raise ValueError(f"time_scale: expected one of {SCALES}, got {self.scale!r}")

    def _candidates(self):
        if self.scale == "utc":
            return ((0, CAPTURE),)
        if self.scale == "tai":
            return ((self.tai_offset_ns, CAPTURE_TAI),)
        return ((0, CAPTURE), (self.tai_offset_ns, CAPTURE_TAI))

    def plausible(self, utc_ns: int, arrival_ns: int) -> bool:
        return -self.max_ahead_ns <= arrival_ns - utc_ns <= self.max_age_ns

    def normalize(self, stamp_ns, arrival_ns: int) -> Tuple[int, str]:
        """(utc_ns, basis) for one frame: the time it is matched and named on, and why."""
        if self.basis == "arrival" or not stamp_ns or stamp_ns <= 0:
            return int(arrival_ns), PLUGIN_ARRIVAL
        stamp_ns = int(stamp_ns)
        candidates = self._candidates()
        for offset, label in candidates:
            if self.plausible(stamp_ns - offset, arrival_ns):
                return stamp_ns - offset, label
        if self.basis == "capture" or self.playback:
            offset, label = candidates[0]   # trusted as configured, even far from now
            return stamp_ns - offset, label
        return int(arrival_ns), PLUGIN_ARRIVAL
