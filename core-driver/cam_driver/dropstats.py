"""Per-source frame-drop accounting -- so "faithful to what the host received" is PROVABLE,
not merely intended.

The primary signal is FRAME-ID DISCONTINUITY. Each source stamps frames with its own id
(GVSP block/chunk id, v4l2 buffer sequence, RTP seq), so a gap means frames were lost before
the core could log them -- on the link / USB, or because backpressure from a recorder that
fell behind forced drops at the receive boundary. Either way it's the thing that turns a
faithful log from an assumption into something the recording can attest to (the counters are
written into the sidecar). A secondary counter tracks frames the core received but the
pipeline could not enqueue.

Pure logic, no GStreamer -- unit-tested directly.
"""
from __future__ import annotations


class DropStats:
    def __init__(self) -> None:
        self.frames = 0             # frames received + processed by the core
        self.source_gaps = 0        # number of frame-id discontinuities observed
        self.frames_missing = 0     # total frames skipped across those gaps
        self.enqueue_failures = 0   # received but the RECORDING feed could not accept (queue full / push != OK)
        self.publish_drops = 0      # best-effort transport publishes skipped (consumer stalled, queue full)
        self._last_fid = None

    def observe_frame(self, frame_id: int) -> int:
        """Record a received frame; return the gap size (frames missing immediately before it,
        0 if contiguous or first). A backward/equal id (counter reset, wrap, or reorder) resyncs
        without counting a false gap."""
        self.frames += 1
        gap = 0
        if self._last_fid is not None and frame_id > self._last_fid + 1:
            gap = frame_id - self._last_fid - 1
            self.source_gaps += 1
            self.frames_missing += gap
        self._last_fid = frame_id
        return gap

    def note_enqueue_failure(self) -> None:
        self.enqueue_failures += 1

    def note_publish_drop(self) -> None:
        self.publish_drops += 1

    def summary(self) -> dict:
        return {
            "frames": self.frames,
            "source_gaps": self.source_gaps,
            "frames_missing": self.frames_missing,
            "enqueue_failures": self.enqueue_failures,
            "publish_drops": self.publish_drops,
        }
