"""The capture-source seam.

A `Source` owns the camera frontend: device lifecycle, the per-frame timestamp policy,
and a feeder that delivers (FrameStamp, image_bytes) to the pipeline via the on_frame
callback. Everything from the pipeline's appsrc onward -- tee -> recorder / transport
(unixfd|header) / raw shm / preview, the sidecar, PTS handling -- is shared and
source-agnostic (see pipeline.CapturePipeline).

GVSP (Aravis) is the only implementation today; a USB/v4l2 source slots in here by
producing FrameStamps from its own clock (SOF/arrival) through the SAME callback, so the
per-frame timestamp-provenance handling downstream stays single-sourced.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Tuple

from ..timestamps import FrameStamp

# The pipeline hands the source this callback; the source invokes it once per delivered
# frame with the resolved stamp + clean image bytes (any device padding/chunk data stripped).
OnFrame = Callable[[FrameStamp, bytes], None]


class Source(ABC):
    # ---- introspection (valid after open() + configure()) ------------------
    @abstractmethod
    def geometry(self) -> Tuple[int, int, int, int]:
        """(x, y, width, height) of the delivered frame."""

    @abstractmethod
    def pixel_format(self) -> str:
        """Pixel-format string (Aravis-style, e.g. 'Mono8'/'BayerRG8') used to derive caps."""

    @property
    def tick_frequency_hz(self) -> int:
        """Camera timestamp tick frequency, for the sidecar header; 0 if N/A."""
        return 0

    @property
    def ptp_locked(self) -> bool:
        """Whether the active timestamp is PTP-disciplined wall-clock (sidecar provenance)."""
        return False

    @property
    @abstractmethod
    def active_timestamp_source(self) -> str:
        """Resolved provenance for the sidecar (e.g. 'ptp_chunk'|'camera'|'system')."""

    # ---- lifecycle ---------------------------------------------------------
    @abstractmethod
    def open(self) -> None:
        """Discover + connect the device."""

    @abstractmethod
    def configure(self) -> None:
        """Apply settings, set up the timestamp policy, and ready the capture stream."""

    @abstractmethod
    def start(self, on_frame: OnFrame) -> None:
        """Arm the feeder (delivering frames to on_frame) and begin acquisition.
        Also used to re-arm after reopen()."""

    @abstractmethod
    def stop(self) -> None:
        """Stop acquisition (best-effort; safe during shutdown / before reopen)."""

    # ---- reconnect (optional; default = unsupported) -----------------------
    @property
    def reconnect_enabled(self) -> bool:
        return False

    def is_disconnected(self) -> bool:
        """True if the link is lost or no frame has arrived within the source's timeout."""
        return False

    def reopen(self) -> None:
        """Re-establish the device after a disconnect; raise if it isn't back yet.
        The caller re-arms the feeder via start() afterwards."""
        raise NotImplementedError
