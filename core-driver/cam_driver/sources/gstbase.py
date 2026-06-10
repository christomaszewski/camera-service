"""Shared machinery for sources that run their OWN GStreamer mini-pipeline feeding appsink(s)
-- USB/v4l2 and RTSP. A source feeds an appsink named `rawsink` (raw frames -> on_frame); an
ENCODED source also tees the encoded stream to a second appsink `encsink` (-> on_encoded) for
the stream-copy recorder, while the decode branch feeds rawsink for live consumers.

Correlation: a pad probe on the PRE-TEE pad stamps each frame exactly once, in order, keyed by
buffer PTS, so both branches deliver the SAME FrameStamp for a frame (decoder reorder is handled
because the lookup is by PTS). Subclasses supply `_pipeline_desc()` + geometry / pixel_format /
encoded_caps / encoded_parser. (HW decode + RTP-NTP/SOF timestamp provenance are follow-ups;
this base stamps arrival time.)
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from ..timestamps import FrameStamp, TimestampSource
from .base import OnEncoded, OnFrame, Source

log = logging.getLogger(__name__)


class GstPipelineSource(Source):
    def __init__(self) -> None:
        self._pipeline: Optional[Gst.Pipeline] = None
        self._rawsink: Optional[Gst.Element] = None
        self._encsink: Optional[Gst.Element] = None
        self._on_frame: Optional[OnFrame] = None
        self._on_encoded: Optional[OnFrame] = None
        self._frame_id = 0
        self._stamps: "OrderedDict[int, FrameStamp]" = OrderedDict()  # pts -> stamp (bounded)
        # Reconnect/liveness: a source feeding its OWN pipeline (rtsp/usb) has no Aravis-style
        # control-lost signal, so we detect a dead/stalled link by DATA STARVATION -- no frame for
        # _reconnect_timeout_s after start. This catches the nasty RTSP case where the camera accepts
        # DESCRIBE/SETUP/PLAY (all 200) but then streams no media (RTP-over-TCP stall / session
        # exhaustion) and the pipeline sits at 0 frames forever. Subclasses opt in (RtspSource sets
        # these from config); the pipeline watchdog then drives is_disconnected()->reopen()->start().
        self._reconnect = False
        self._reconnect_timeout_s = 5.0    # mid-stream stall: max gap between frames before reopen
        self._first_frame_grace_s = 12.0   # longer window for the FIRST frame after start (a healthy 4K
        #                                    stream needs a few s to connect + deliver its first AU)
        self._last_data_ns = 0       # arrival of the most recent frame (set in _new_stamp); 0 = none yet
        self._start_ns = 0           # when the current capture attempt began (start())
        self._started = False

    # ---- subclass hook -----------------------------------------------------
    def _pipeline_desc(self) -> str:
        """gst-launch string with an `appsink name=rawsink`; an encoded source also has a
        `tee name=st` and an `appsink name=encsink` off it."""
        raise NotImplementedError

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        Gst.init(None)

    def configure(self) -> None:
        desc = self._pipeline_desc()
        log.info("%s pipeline: %s", type(self).__name__, desc)
        self._pipeline = Gst.parse_launch(desc)
        self._rawsink = self._pipeline.get_by_name("rawsink")
        self._encsink = self._pipeline.get_by_name("encsink")   # None for a raw source
        if self._rawsink is None:
            raise RuntimeError(f"{type(self).__name__}: appsink 'rawsink' not found")
        if self.encoded_caps is not None:
            tee = self._pipeline.get_by_name("st")
            if tee is None:
                raise RuntimeError(f"{type(self).__name__}: an encoded source needs a 'tee name=st'")
            tee.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._stamp_probe)
        self._configure_pipeline()

    def _configure_pipeline(self) -> None:
        """Post-parse element tweaks for properties that can't live in the launch string because they
        may not exist on the running GStreamer (e.g. rtspsrc add-reference-timestamp-meta on gst<1.24).
        Default no-op; RtspSource overrides to enable the NTP reference meta when available."""

    @staticmethod
    def _hw_decode_available() -> bool:
        """True when the Jetson HW decoder (nvv4l2decoder) is present, so the consumer decode branch
        uses NVDEC + nvvidconv; False on dev/x86/non-Jetson -> software decode. Same image auto-upgrades
        to HW on a JP7 host (with the GPU exposed) -- no code change. See formats.select_decoder."""
        return Gst.ElementFactory.find("nvv4l2decoder") is not None

    def start(self, on_frame: OnFrame, on_encoded: OnEncoded = None) -> None:
        self._on_frame = on_frame
        self._on_encoded = on_encoded
        self._start_ns = time.time_ns()   # liveness baseline: the first frame must arrive within timeout
        self._last_data_ns = 0
        self._started = True
        self._rawsink.connect("new-sample", self._on_raw)
        if self._encsink is not None:
            self._encsink.connect("new-sample", self._on_enc)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        self._started = False           # suspend the liveness watchdog while torn down (e.g. mid-reopen)
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)

    # ---- reconnect (data-starvation watchdog; opt-in via subclass) ----------
    @property
    def reconnect_enabled(self) -> bool:
        return self._reconnect

    def is_disconnected(self) -> bool:
        """Disconnected == data starvation. A LONGER startup grace (_first_frame_grace_s) runs from
        start() until the first frame -- a healthy 4K stream can take a few seconds to connect + deliver
        its first AU, so we must not false-trip -- yet a camera that ACKs PLAY but never streams media is
        still caught once the grace elapses. After the first frame, the SHORTER inter-frame timeout
        (_reconnect_timeout_s) detects a mid-stream stall. Always False unless a subclass opted in."""
        if not self._reconnect or not self._started:
            return False
        now = time.time_ns()
        if self._last_data_ns == 0:                                   # no frame yet: startup grace window
            stalled = (now - self._start_ns) > int(self._first_frame_grace_s * 1e9)
            if stalled:
                log.warning("liveness: no frame %.1fs after start (grace %.1fs) -> reopening stream",
                            (now - self._start_ns) / 1e9, self._first_frame_grace_s)
            return stalled
        stalled = (now - self._last_data_ns) > int(self._reconnect_timeout_s * 1e9)
        if stalled:
            log.warning("liveness: stream stalled %.1fs (timeout %.1fs, %d frame(s) so far) -> reopening",
                        (now - self._last_data_ns) / 1e9, self._reconnect_timeout_s, self._frame_id)
        return stalled

    def reopen(self) -> None:
        """Rebuild the mini-pipeline from scratch (NULL -> re-parse -> reattach the stamp probe). For
        RTSP this re-runs DESCRIBE/SETUP/PLAY on a FRESH session -- the reliable way to clear a camera's
        stalled stream. Reuses the codec/geometry resolved at open(); RtspSource overrides this to
        re-probe first and raise SourceConfigChanged when the stream no longer matches what the
        pipelines were built for (the probe runs while the old session is torn down, so it doesn't
        contend with a live stream). The caller re-arms via start(). Raises (caught by the pipeline's
        backoff loop) if re-parse fails."""
        self.stop()
        self.configure()

    def _pts_to_realtime(self, pts):
        """Map a buffer running-time PTS to CLOCK_REALTIME ns via the live pipeline-clock offset (the
        pipeline clock is monotonic, == CLOCK_REALTIME minus a near-constant offset). None if the clock
        isn't ready. Used by USB SOF: with do-timestamp=false, buf.pts is the v4l2 driver's per-frame
        capture time, and this lifts it into the wall-clock epoch the rest of the stamps use."""
        if self._pipeline is None:
            return None
        clock = self._pipeline.get_clock()
        base = self._pipeline.get_base_time()
        if clock is None or base == Gst.CLOCK_TIME_NONE:
            return None
        run_now = clock.get_time() - base
        return time.time_ns() - (run_now - int(pts))

    # ---- capture-timestamp extraction (subclass hook) ----------------------
    def _extract_capture(self, buf):
        """Return (capture_ns, provenance) read off the buffer, or (None, SYSTEM) to use arrival.
        Subclasses override to pull a sensor-closer stamp (RTSP: RTCP->NTP reference-timestamp-meta;
        USB: v4l2 SOF on hardware). Called pre-tee for encoded sources (so both branches inherit it
        via the PTS-keyed lookup) and on the raw buffer otherwise. Default = arrival (SYSTEM)."""
        return None, TimestampSource.SYSTEM

    # ---- stamping / correlation --------------------------------------------
    def _new_stamp(self, buf) -> FrameStamp:
        now = time.time_ns()                          # host arrival (CLOCK_REALTIME)
        self._last_data_ns = now                      # liveness: a frame arrived (drives is_disconnected)
        capture_ns, prov = self._extract_capture(buf)
        if capture_ns is not None:
            ts, src, cam = int(capture_ns), prov, int(capture_ns)
        else:
            pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else now
            ts, src, cam = now, TimestampSource.SYSTEM, pts
        st = FrameStamp(frame_id=self._frame_id, timestamp_ns=ts, source=src,
                        system_ns=now, camera_ns=cam, chunk_ns=None)
        self._frame_id += 1
        return st

    def _stamp_probe(self, pad, info) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else time.time_ns()
        self._stamps[pts] = self._new_stamp(buf)
        while len(self._stamps) > 240:
            self._stamps.popitem(last=False)   # bounded; evict oldest
        return Gst.PadProbeReturn.OK

    def _stamp_for(self, buf) -> FrameStamp:
        """Look up the pre-tee stamp by buffer PTS (both branches share it). On a MISS, reuse the
        most-recent pre-tee stamp -- do NOT mint a new one. A decode-branch parser/decoder can shift
        PTS (e.g. jpegparse re-times MJPEG), so the decoded buffer won't match the pre-tee key; minting
        a fresh stamp there would advance the shared frame-id counter the pre-tee probe owns, double-
        counting frames and inflating the drop stats (it looked like a 2x camera over-rate). The decode
        branch is best-effort consumer pixels; the encoded/recording branch is queue-only (PTS intact),
        so it always hits and the recording keeps exact per-frame ids."""
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
        st = self._stamps.get(pts) if pts is not None else None
        if st is not None:
            return st
        if self._stamps:
            return next(reversed(self._stamps.values()))   # most-recent pre-tee stamp; no counter bump
        return self._new_stamp(buf)                          # nothing stamped yet (very first frame)

    def _on_raw(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        # encoded: correlate with the pre-tee stamp (carries the NTP meta); raw: stamp this buffer
        stamp = self._stamp_for(buf) if self.encoded_caps is not None else self._new_stamp(buf)
        data = buf.extract_dup(0, buf.get_size())
        if self._on_frame is not None:
            self._on_frame(stamp, data)
        return Gst.FlowReturn.OK

    def _on_enc(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        if self._on_encoded is not None:
            # carry the negotiated caps (stream-format + codec_data) so the recorder appsrc can mux
            caps = sample.get_caps()
            self._on_encoded(self._stamp_for(buf), data, caps.to_string() if caps else None)
        return Gst.FlowReturn.OK

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SYSTEM.value
