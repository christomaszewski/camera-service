"""usbmon-capture playback source: replay a Wireshark-on-Linux pcap/pcapng of a USB
(UVC) camera through the service.

The uvcpcap parser reassembles the capture's UVC payload transfers back into frames;
this source delivers them with stamps built from the CAPTURE timestamps (provenance
'system' -- host arrival, at capture time), paced/retimed/looped per config (shared
semantics: cam_driver.playback).

RAW payloads (the primary target -- e.g. a 16-bit thermal core's uncompressed Y16 ->
GRAY16_LE): a plain feeder thread (GigE-feeder style, no GStreamer involved) calls
on_frame directly.

MJPEG payloads: the reassembled JPEGs feed an appsrc-headed mini-pipeline with the
base's dual-output tee -- decode branch for consumers, encsink for the stream-copy
recorder -- so a pcap of an MJPEG cam records byte-faithful JPEGs, exactly like the
live UsbSource would.

Config pins pixel_format/width/height (like a real UVC cam); the parser validates
reassembled frames against them and fails legibly on mismatch. bus/device/endpoint
pin the usbmon stream when auto-detection would pick wrong (multiple cameras).
"""
from __future__ import annotations

import logging
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .. import playback, uvcpcap
from ..formats import bytes_per_frame, encoded_info, parse_pixel_format, select_decoder
from ..timestamps import FrameStamp, TimestampSource
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)


class PcapSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.PcapConfig
        super().__init__()
        self.cfg = cfg
        pf = (cfg.pixel_format or "").upper()
        if pf in ("H264", "H265"):
            raise ValueError(
                "pcap source: UVC H.264/H.265 frame-based payloads are not supported "
                "(known: raw formats e.g. GRAY16_LE/GRAY8/YUY2, and MJPEG)")
        self._mjpeg = pf in ("MJPEG", "JPEG")
        self._enc = encoded_info(pf) if self._mjpeg else None
        if not self._mjpeg:
            self._gst_format = parse_pixel_format(cfg.pixel_format)[0]
            self._expected = bytes_per_frame(self._gst_format, cfg.width, cfg.height)
        self._extractor = None      # built in open(), re-iterated per loop cycle
        self._stream = None         # the probed StreamInfo (logging + fps hint)
        self._pacer = playback.Pacer(cfg.speed)
        self._retime_offset = 0
        self._finished = False
        self._failed = False
        self._stop_evt = threading.Event()
        self._thread = None
        self._appsrc = None         # mjpeg only
        self._stamp_q = None        # mjpeg only: feed-order stamps for the pre-tee probe
        self._base_src_ts = None    # mjpeg only: buffer PTS zero point

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        super().open()
        pr = uvcpcap.probe(self.cfg.path)
        self._stream = uvcpcap.select_stream(
            pr, self.cfg.bus, self.cfg.device, self.cfg.endpoint)
        log.info("pcap source: %s (%s), stream %s", self.cfg.path, pr.file_format,
                 self._stream.describe())
        for s in pr.streams:
            if s is not self._stream:
                log.debug("pcap: other stream: %s", s.describe())
        neg = self._stream.negotiated
        if not self._mjpeg and neg and neg.max_video_frame_size and \
                neg.max_video_frame_size != self._expected:
            log.warning("pcap: the capture's VS_COMMIT negotiated dwMaxVideoFrameSize=%d but "
                        "pcap.width/height/pixel_format imply %d -- if replay fails on frame "
                        "size, fix the config to what the camera actually streamed",
                        neg.max_video_frame_size, self._expected)
        if self._stream.described:
            fmt, w, h = self._stream.described
            if w != self.cfg.width or h != self.cfg.height:
                log.warning("pcap: the capture's enumeration describes %s %dx%d; config says "
                            "%s %dx%d", fmt, w, h, self.cfg.pixel_format,
                            self.cfg.width, self.cfg.height)
        if (self.cfg.retime or "original") not in ("original", "wall"):
            raise ValueError(f"pcap.retime: expected 'original' or 'wall', got {self.cfg.retime!r}")
        # (retime=wall resolves its offset lazily, off the first frame's capture timestamp)
        self._extractor = uvcpcap.UvcFrameExtractor(
            self.cfg.path, bus=self._stream.bus, dev=self._stream.dev, ep=self._stream.ep,
            expected_frame_size=None if self._mjpeg else self._expected,
            mjpeg=self._mjpeg,
            max_payload_size=neg.max_payload_size if neg else None)

    def configure(self) -> None:
        if self._mjpeg:
            super().configure()   # parse the mini-pipeline, hook the pre-tee stamp probe
            self._appsrc = self._pipeline.get_by_name("pcap_src")
            self._stamp_q = []
            bus = self._pipeline.get_bus()
            bus.add_signal_watch()
            # EOF: the feeder pushes appsrc EOS; `finished` only once the bus posts EOS --
            # i.e. after the queued tail has DRAINED through encsink (else request_stop's
            # NULL would silently truncate the stream-copy recording by the in-flight frames)
            bus.connect("message::eos", self._on_mini_eos)
            bus.connect("message::error", self._on_mini_error)
        # raw mode: no GStreamer pipeline at all -- the feeder calls on_frame directly

    def _on_mini_eos(self, _bus, _msg) -> None:
        log.info("pcap: mini-pipeline drained")
        self._finished = True

    def _on_mini_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        log.error("pcap mini-pipeline error: %s | %s -- ending playback", err, dbg)
        self._failed = True
        self._finished = True

    def _pipeline_desc(self) -> str:
        w, h, fps = int(self.cfg.width), int(self.cfg.height), 0
        _caps, parser, sw_decoder = self._enc
        decoder, conv = select_decoder(sw_decoder, self._hw_decode_available())
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false"
        return (
            # block=true: a full appsrc queue back-pressures the feeder's push-buffer --
            # bounded memory even at speed=0 (as-fast-as-possible replay)
            f"appsrc name=pcap_src is-live=true do-timestamp=false format=time block=true "
            f"caps=image/jpeg ! tee name=st "
            # decode branch is BEST-EFFORT (consumers); leaky so a slow decoder drops here
            # instead of back-pressuring the must-not-drop stream-copy (encsink) branch
            f"st. ! queue leaky=downstream max-size-buffers=8 ! {parser} ! {decoder} ! {conv} ! "
            f"video/x-raw,format=I420,width={w},height={h} ! {raw_sink} "
            f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
        )

    def start(self, on_frame, on_encoded=None) -> None:
        self._stop_evt.clear()
        if self._mjpeg:
            super().start(on_frame, on_encoded)   # PLAYING + sink callbacks
        else:
            self._on_frame = on_frame
        self._thread = threading.Thread(target=self._feed_loop, name="pcap-feeder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        # mjpeg: NULL the mini-pipeline FIRST -- it flushes the appsrc, unblocking a feeder
        # stuck in a block=true push-buffer; joining before that would always burn the timeout
        if self._mjpeg:
            super().stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning("pcap: feeder thread did not stop within 5s")
            self._thread = None

    # ---- feeder ------------------------------------------------------------
    def _feed_loop(self) -> None:
        cycle, offset, span = 0, 0, 0
        eos_sent = False
        try:
            while not self._stop_evt.is_set() and not self._failed:
                delivered = 0
                for idx, (ts, frame) in enumerate(iter(self._extractor)):
                    if self._stop_evt.is_set() or self._failed:
                        return
                    if self._retime_offset == 0 and cycle == 0 and \
                            (self.cfg.retime or "original") == "wall":
                        self._retime_offset = time.time_ns() - ts
                    ts += self._retime_offset + offset
                    stamp = FrameStamp(frame_id=idx, timestamp_ns=ts,
                                       source=TimestampSource.SYSTEM,
                                       system_ns=ts, camera_ns=ts, chunk_ns=None)
                    self._pacer.wait(ts, cancel=self._stop_evt)
                    if self._stop_evt.is_set():
                        return
                    if self._mjpeg:
                        self._push_encoded(stamp, frame)
                    elif self._on_frame is not None:
                        self._on_frame(stamp, frame)
                    delivered += 1
                st = self._extractor.stats
                if cycle == 0:
                    log.info("pcap: %d frame(s) over %.2fs%s", delivered,
                             (st.last_ts_ns - st.first_ts_ns) / 1e9 if st.last_ts_ns else 0.0,
                             " (looping)" if self.cfg.loop else "")
                    if st.size_drops or st.err_frames or st.truncated or st.bad_header:
                        log.warning("pcap: dropped during reassembly: size=%d err=%d "
                                    "truncated=%d bad_header=%d", st.size_drops,
                                    st.err_frames, st.truncated, st.bad_header)
                if not self.cfg.loop:
                    break
                if span == 0:
                    interval = (st.last_ts_ns - st.first_ts_ns) // max(1, st.frames_ok - 1) \
                        if st.frames_ok > 1 else playback.DEFAULT_INTERVAL_NS
                    span = (st.last_ts_ns - st.first_ts_ns) + interval if st.last_ts_ns else 0
                cycle += 1
                offset = cycle * span
            # clean end of feed. mjpeg: push EOS and let `finished` come from the bus EOS
            # handler AFTER the queued tail drains through encsink -- flipping it here
            # would let request_stop() NULL the mini-pipeline mid-drain (truncated recording)
            if self._mjpeg and not self._stop_evt.is_set() and not self._failed:
                eos_sent = self._appsrc.emit("end-of-stream") == Gst.FlowReturn.OK
                if eos_sent:
                    # bounded drain wait: a stream jpegparse can't parse never negotiates,
                    # so EOS never reaches the bus -- fail loudly instead of hanging forever
                    for _ in range(100):
                        if self._finished or self._stop_evt.wait(0.1):
                            break
                    if not (self._finished or self._stop_evt.is_set()):
                        log.error("pcap: mini-pipeline did not drain within 10s -- were the "
                                  "capture's JPEGs decodable? (%d stamp(s) never reached the "
                                  "pipeline)", len(self._stamp_q))
                        self._failed = True
                        eos_sent = False   # the finally block flips finished
        except ValueError as e:
            # parser-level failure (wrong size/endpoint/format): surface loudly; the
            # watchdog then finalizes whatever was recorded so far -- with a non-zero exit
            log.error("pcap replay failed: %s", e)
            self._failed = True
        finally:
            if not eos_sent:
                self._finished = True

    def _push_encoded(self, stamp: FrameStamp, frame: bytes) -> None:
        """Feed one JPEG into the mini-pipeline; the base's pre-tee probe assigns it the
        stamp queued here (feed order == pre-tee buffer order on a single appsrc)."""
        if self._base_src_ts is None:
            self._base_src_ts = stamp.timestamp_ns
        self._stamp_q.append(stamp)
        buf = Gst.Buffer.new_wrapped(frame)
        buf.pts = stamp.timestamp_ns - self._base_src_ts   # unique, monotonic (loop-shifted)
        buf.dts = Gst.CLOCK_TIME_NONE
        buf.offset = stamp.frame_id
        if self._appsrc.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            log.warning("pcap: mini-pipeline rejected a buffer (flushing?)")
            # the pre-tee probe will never see this buffer: take its stamp back out, or
            # every later frame would be stamped one frame early (FIFO shift)
            if self._stamp_q and self._stamp_q[-1] is stamp:
                self._stamp_q.pop()

    def _new_stamp(self, buf) -> FrameStamp:
        # mjpeg pre-tee probe: consume the feed-order stamp queue instead of minting
        self._last_data_ns = time.time_ns()
        if self._stamp_q:
            return self._stamp_q.pop(0)
        return super()._new_stamp(buf)   # unreachable in practice; safe fallback

    # ---- EOF ---------------------------------------------------------------
    @property
    def finite(self) -> bool:
        return True   # even loop mode ends on a parser error

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def finished_error(self) -> bool:
        return self._failed

    @property
    def delivered_frame_rate(self):
        if self.cfg.frame_rate:
            return float(self.cfg.frame_rate)
        neg = self._stream.negotiated if self._stream else None
        return neg.fps if neg and neg.fps else None

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        return "I420" if self._mjpeg else self.cfg.pixel_format

    @property
    def encoded_caps(self):
        return self._enc[0] if self._enc else None

    @property
    def encoded_parser(self):
        return self._enc[1] if self._enc else None

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SYSTEM.value
