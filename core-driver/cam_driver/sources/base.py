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
from typing import Callable, Optional, Tuple

from ..timestamps import FrameStamp

# The pipeline hands the source this callback; the source invokes it once per delivered
# frame with the resolved stamp + clean image bytes (any device padding/chunk data stripped).
OnFrame = Callable[[FrameStamp, bytes], None]

# Encoded delivery passes the SAMPLE's caps string too (stream-format + codec_data -- e.g. the
# H.264/H.265 VPS/SPS/PPS that hvc1/avc carry in caps, not in the bytes), so the stream-copy
# recorder's appsrc negotiates with the muxer. Raw bytes alone would drop the parameter sets.
OnEncoded = Callable[[FrameStamp, bytes, Optional[str]], None]


class SourceConfigChanged(RuntimeError):
    """Raised by reopen() when the source's delivered format (codec/geometry) no longer matches
    what the pipelines were built for. A reopen can't fix this in-process -- the main pipeline's
    appsrc caps are fixed at build() -- so the pipeline treats it as fatal: finalize the recording
    cleanly and exit non-zero; the supervisor/compose restart rebuilds for the new format."""


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

    # ---- encoded delivery (optional; raw by default) -----------------------
    @property
    def encoded_caps(self):
        """Caps of the delivered ENCODED bitstream (e.g. 'image/jpeg', 'video/x-h264') for the
        stream-copy recorder's appsrc, or None for a raw source. An encoded source ALSO decodes
        and delivers raw frames via on_frame (so transport/consumers are unchanged)."""
        return None

    @property
    def encoded_parser(self):
        """GStreamer parser the stream-copy recorder muxes through (e.g. 'jpegparse'), or None."""
        return None

    # ---- lifecycle ---------------------------------------------------------
    @abstractmethod
    def open(self) -> None:
        """Discover + connect the device."""

    @abstractmethod
    def configure(self) -> None:
        """Apply settings, set up the timestamp policy, and ready the capture stream."""

    @abstractmethod
    def start(self, on_frame: OnFrame, on_encoded: "OnEncoded" = None) -> None:
        """Arm the feeder and begin acquisition (also used to re-arm after reopen()). Delivers
        raw frames to on_frame. An encoded source (encoded_caps set) ALSO delivers the encoded
        bitstream (+ its caps string) to on_encoded -- when the pipeline supplies it -- for the
        stream-copy recorder."""

    @abstractmethod
    def stop(self) -> None:
        """Stop acquisition (best-effort; safe during shutdown / before reopen)."""

    def close(self) -> None:
        """Release the device at END OF LIFE (after the pipeline is torn down). Distinct from
        stop(): stop() may keep device control for a fast reconnect; close() must surrender it
        deterministically (e.g. GigE control privilege) rather than relying on interpreter-exit
        GC. Default: nothing to release beyond stop()."""

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
