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
from .base import OnFrame, Source

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

    def start(self, on_frame: OnFrame, on_encoded: OnFrame = None) -> None:
        self._on_frame = on_frame
        self._on_encoded = on_encoded
        self._rawsink.connect("new-sample", self._on_raw)
        if self._encsink is not None:
            self._encsink.connect("new-sample", self._on_enc)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)

    # ---- stamping / correlation --------------------------------------------
    def _new_stamp(self, pts_hint: int) -> FrameStamp:
        now = time.time_ns()   # arrival; SOF (USB) / RTP-NTP (RTSP) provenance are follow-ups
        st = FrameStamp(frame_id=self._frame_id, timestamp_ns=now, source=TimestampSource.SYSTEM,
                        system_ns=now, camera_ns=pts_hint, chunk_ns=None)
        self._frame_id += 1
        return st

    def _stamp_probe(self, pad, info) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else time.time_ns()
        self._stamps[pts] = self._new_stamp(pts)
        while len(self._stamps) > 240:
            self._stamps.popitem(last=False)   # bounded; evict oldest
        return Gst.PadProbeReturn.OK

    def _stamp_for(self, buf) -> FrameStamp:
        """Look up the pre-tee stamp by buffer PTS (both branches share it); fall back if evicted."""
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
        st = self._stamps.get(pts) if pts is not None else None
        return st if st is not None else self._new_stamp(pts or time.time_ns())

    def _on_raw(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        if self.encoded_caps is not None:
            stamp = self._stamp_for(buf)   # correlate with the encoded branch
        else:
            stamp = self._new_stamp(int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else time.time_ns())
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
            self._on_encoded(self._stamp_for(buf), data)
        return Gst.FlowReturn.OK

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SYSTEM.value
