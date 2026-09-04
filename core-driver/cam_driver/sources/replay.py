"""Recorded-run playback source (a GstPipelineSource).

Feeds the service from a run THIS service previously recorded: `<prefix>-NNNNN.mkv`
parts read seamlessly by splitmuxsrc (the reader pair of the splitmuxsink that wrote
them) + the sidecar CSV/JSON to re-stamp every frame with its ORIGINAL FrameStamp
(frame_id, timestamps, provenance) -- so recording, transport, and plugins downstream
behave as if the original camera were live.

Two shapes, resolved by probing the first .mkv part against the sidecar header:

  LOSSLESS RAW runs (ffv1 / hw-hevc-lossless / x265-lossless):
      splitmuxsrc ! decoder ! videoconvert ! <header format> ! rawsink -> on_frame
    Decoded back to the exact recorded bytes (the codecs are lossless); a CFA-tiled
    recording is un-tiled per frame (bayer_tile.untile_cfa, the exact inverse) so
    consumers see the original mosaic.

  STREAM-COPY runs (USB-MJPEG / RTSP H.26x, header pixel_format == I420):
      splitmuxsrc ! tee -> decode branch -> rawsink (consumers)
                       -> encsink (on_encoded)      -- the recording stream-copies AGAIN,
    byte-faithful to the original capture, via the base's dual-output machinery.

Pacing/retime/loop semantics are shared with the pcap source -- see cam_driver.playback.
EOF: splitmuxsrc posts EOS on the mini-pipeline bus -> `finished` (the main pipeline
watchdog finalizes and exits 0), or a flushing seek back to 0 when `loop` is on.
(Loop + stream-copy note: PTS values repeat each cycle, so the best-effort decode
branch's stamp correlation may briefly cross cycles; the recording branch is in-order.)
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from .. import playback
from ..bayer_tile import normalize_mode, untile_cfa
from ..formats import encoded_info, select_decoder
from ..timestamps import FrameStamp, TimestampSource
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)

# mkv track caps -> the software decode chain for the LOSSLESS RAW path (correctness
# first: bit-exactness is the point of replay, and these all hold real-time here).
_RAW_DECODE = {
    "video/x-ffv": "avdec_ffv1",   # FFV1: the caps NAME is x-ffv (version rides `ffvversion`)
    "video/x-h265": "h265parse ! avdec_h265",
    "video/x-h264": "h264parse ! avdec_h264",
    "image/jpeg": "jpegdec",
}
# mkv track caps -> formats.encoded_info key for the STREAM-COPY path.
_ENC_KEY = {"image/jpeg": "MJPEG", "video/x-h264": "H264", "video/x-h265": "H265"}


def _probe_mkv(path: str, timeout_s: int = 5):
    """Read the first part's video-track caps name from the matroska headers (PAUSED is
    enough -- no decode, no full-file scan). Returns e.g. 'video/x-ffv', or None."""
    try:
        pipeline = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux name=d')
    except Exception as e:
        log.warning("replay probe of %s failed to build: %s", path, e)
        return None
    result = {}
    loop = GLib.MainLoop()

    def on_pad(_d, pad):
        caps = pad.get_current_caps()
        s = caps.get_structure(0) if caps and caps.get_size() else None
        if s and (s.get_name().startswith("video/") or s.get_name().startswith("image/")):
            result["caps"] = s.get_name()
            loop.quit()

    pipeline.get_by_name("d").connect("pad-added", on_pad)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", lambda *_: loop.quit())
    timeout_id = GLib.timeout_add_seconds(timeout_s, loop.quit)
    pipeline.set_state(Gst.State.PAUSED)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    # tidy the DEFAULT main context: the service's real main loop runs on it later, so a
    # leftover timeout/watch would fire (and hold this closure alive) inside the live run
    src = GLib.MainContext.default().find_source_by_id(timeout_id)
    if src is not None and not src.is_destroyed():
        GLib.source_remove(timeout_id)   # quit came from on_pad/error; the timeout is pending
    bus.remove_signal_watch()
    return result.get("caps")


class ReplaySource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.ReplayConfig
        super().__init__()
        self.cfg = cfg
        self._run: playback.RunInfo = None
        self._csv_stamps = []
        self._idx = 0
        self._cycle = 0
        self._offset = 0            # retime + loop shift applied to every delivered stamp
        self._retime_offset = 0
        self._span_ns = 0           # cycle length: last-first + one median interval
        self._median_ns = playback.DEFAULT_INTERVAL_NS
        self._pacer = playback.Pacer(cfg.speed)
        self._stop_evt = threading.Event()   # cancels a pacing sleep (recorded gaps can be seconds)
        self._finished = False
        self._failed = False
        self._synth_warned = False
        self._mkv_caps = None
        self._enc = None            # (caps, parser, decoder) for a stream-copy run
        self._untile = None         # frame-bytes transform for CFA-tiled runs

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        super().open()
        self._run = playback.discover_run(self.cfg.path, getattr(self.cfg, "run", ""))
        hdr = self._run.header
        self._csv_stamps = playback.load_stamps(self._run.csv_path)
        if not self._csv_stamps:
            raise ValueError(f"replay: {self._run.csv_path} has no frame rows")
        ts = [s.timestamp_ns for s in self._csv_stamps]
        self._median_ns = playback.median_interval_ns(ts)
        self._span_ns = (ts[-1] - ts[0]) + self._median_ns
        if (self.cfg.retime or "original") == "wall":
            self._retime_offset = time.time_ns() - ts[0]
        elif self.cfg.retime not in ("", "original"):
            raise ValueError(f"replay.retime: expected 'original' or 'wall', got {self.cfg.retime!r}")
        self._offset = self._retime_offset

        # splitmuxsrc matches parts with a simple `*` glob (GPatternSpec: no escapes, no
        # character classes) -- refuse the shapes it would silently mis-match rather than
        # deliver a sibling run's frames under this run's stamps.
        if any(ch in self._run.base for ch in "*?[]"):
            raise ValueError(f"replay: run path {self._run.base!r} contains glob characters "
                             f"splitmuxsrc cannot handle -- rename/move the run")
        run_dir = os.path.dirname(self._run.base)
        over = sorted(set(fnmatch.filter(os.listdir(run_dir),
                                         os.path.basename(self._run.base) + "-*.mkv"))
                      - {os.path.basename(p) for p in self._run.mkv_paths})
        if over:
            raise ValueError(
                f"replay: {self._run.mkv_glob} would also match other files ({over[0]}, ...) -- "
                f"another run's prefix extends this one; move the runs into separate directories")

        self._mkv_caps = _probe_mkv(self._run.mkv_paths[0])
        if self._mkv_caps is None:
            raise ValueError(f"replay: {self._run.mkv_paths[0]} has no readable video track")
        # Stream-copy runs recorded the DELIVERED bitstream; their sidecar header carries the
        # decoded consumer format (I420). Everything else is a lossless re-encode of raw frames.
        stream_copy = self._mkv_caps in _ENC_KEY and hdr.get("pixel_format") == "I420"
        if stream_copy:
            self._enc = encoded_info(_ENC_KEY[self._mkv_caps])
        elif self._mkv_caps not in _RAW_DECODE:
            raise ValueError(f"replay: unsupported recorded codec {self._mkv_caps!r} "
                             f"in {self._run.mkv_paths[0]}")

        tile_mode = normalize_mode(hdr.get("cfa_tile_mode", "off"))
        if tile_mode != "off" and not stream_copy:
            w, h = int(hdr["width"]), int(hdr["height"])
            pattern = hdr.get("bayer_pattern") or "rggb"
            self._untile = lambda b: untile_cfa(b, w, h, mode=tile_mode, pattern=pattern)
            log.info("replay: CFA-tiled recording (%s/%s) -- un-tiling to the original mosaic",
                     tile_mode, pattern)
        log.info("replay source: %s (%d frames, %d part(s), %s -> %s, ~%.1f fps%s%s)",
                 self._run.base, len(self._csv_stamps), len(self._run.mkv_paths), self._mkv_caps,
                 "stream-copy" if stream_copy else hdr.get("pixel_format"),
                 self.delivered_frame_rate or 0.0,
                 f", speed x{self.cfg.speed:g}" if self.cfg.speed not in (0.0, 1.0) else "",
                 ", loop" if self.cfg.loop else "")

    def configure(self) -> None:
        super().configure()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)

    def start(self, on_frame, on_encoded=None) -> None:
        if self._untile is not None:
            inner, untile = on_frame, self._untile
            on_frame = lambda st, data: inner(st, untile(data))   # noqa: E731
        super().start(on_frame, on_encoded)

    # ---- mini-pipeline -----------------------------------------------------
    def _pipeline_desc(self) -> str:
        hdr = self._run.header
        w, h = int(hdr["width"]), int(hdr["height"])
        src = f'splitmuxsrc location="{self._run.mkv_glob}"'
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=8 drop=false sync=false"
        if self._enc:
            _caps, parser, sw_decoder = self._enc
            decoder, conv = select_decoder(sw_decoder, self._hw_decode_available(),
                                           getattr(self.cfg, "decoder", "auto"))
            return (
                f"{src} ! tee name=st "
                # decode branch is BEST-EFFORT (consumers); leaky so a slow decoder drops
                # here instead of stalling the must-not-drop stream-copy branch below
                f"st. ! queue leaky=downstream max-size-buffers=8 ! {parser} ! {decoder} ! {conv} ! "
                f"video/x-raw,format=I420,width={w},height={h} ! {raw_sink} "
                f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
            )
        gst_format = self._container_format(hdr)
        return (f"{src} ! {_RAW_DECODE[self._mkv_caps]} ! videoconvert n-threads=2 ! "
                f"video/x-raw,format={gst_format},width={w},height={h} ! {raw_sink}")

    @staticmethod
    def _container_format(hdr: dict) -> str:
        """The GStreamer raw format the recording actually rode in. Aravis-style names
        (Mono8/BayerRG8/Mono16) ride GRAY8/GRAY16_LE containers; GStreamer-native ones
        (GRAY8/GRAY16_LE/I420/...) are literal. This mirrors formats.parse_pixel_format
        without re-deriving bayer/bit metadata the header already carries."""
        pf = hdr.get("pixel_format") or "GRAY8"
        from ..formats import parse_pixel_format
        return parse_pixel_format(pf)[0]

    # ---- re-stamping (frame N of the recording = CSV row N) ----------------
    def _new_stamp(self, buf) -> FrameStamp:
        self._last_data_ns = time.time_ns()
        idx = self._idx
        self._idx += 1
        if idx < len(self._csv_stamps):
            st = playback.shift_stamp(self._csv_stamps[idx], self._offset)
        else:
            # more frames in the .mkv than CSV rows (e.g. a crash cut the sidecar short):
            # keep delivering with synthesized stamps rather than dying mid-replay
            if not self._synth_warned:
                self._synth_warned = True
                log.warning("replay: recording has more frames than %s has rows -- "
                            "synthesizing stamps from row %d on", self._run.csv_path, idx)
            last = playback.shift_stamp(self._csv_stamps[-1], self._offset)
            ts = last.timestamp_ns + (idx - len(self._csv_stamps) + 1) * self._median_ns
            st = FrameStamp(frame_id=last.frame_id + (idx - len(self._csv_stamps) + 1),
                            timestamp_ns=ts, source=TimestampSource.SYSTEM,
                            system_ns=ts, camera_ns=ts, chunk_ns=None)
        # blocks the streaming thread = natural backpressure; stop() cancels the sleep
        # (a recorded gap can be seconds long and must not stall shutdown)
        self._pacer.wait(st.timestamp_ns, cancel=self._stop_evt)
        return st

    def stop(self) -> None:
        self._stop_evt.set()   # wake a pacing sleep so set_state(NULL) isn't blocked by it
        super().stop()

    # ---- EOF / loop --------------------------------------------------------
    def _check_row_count(self) -> None:
        """Frame N of the recording is re-stamped from CSV row N (ordinal, not keyed), which holds
        only while every frame the sidecar attests is actually delivered. Fewer frames than rows
        means a frame went missing somewhere in the decode path -- and every row after it was
        attached to the wrong frame. Say so, loudly, rather than let a silently mis-stamped
        reprocess look complete. (More frames than rows is handled in _new_stamp.)"""
        rows = len(self._csv_stamps)
        if 0 < self._idx < rows:
            log.warning("replay: %s has %d rows but only %d frames were delivered -- rows after the "
                        "first missing frame were attached to the wrong frames; the re-recorded "
                        "stamps are NOT trustworthy for this run", self._run.csv_path, rows, self._idx)

    def _on_eos(self, _bus, _msg) -> None:
        self._check_row_count()
        if not self.cfg.loop:
            log.info("replay: end of run (%d frames delivered)", self._idx)
            self._finished = True
            return
        self._cycle += 1
        self._idx = 0
        self._offset = self._retime_offset + self._cycle * self._span_ns
        log.info("replay: loop -> cycle %d (timestamps shifted %+.3fs)",
                 self._cycle, self._cycle * self._span_ns / 1e9)
        if not self._pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0):
            log.error("replay: loop seek failed (parts moved/deleted?) -- ending playback")
            self._failed = True
            self._finished = True

    def _on_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        log.error("replay pipeline error: %s | %s -- ending playback", err, dbg)
        self._failed = True     # surfaces as a non-zero exit; recording finalizes regardless
        self._finished = True

    @property
    def finite(self) -> bool:
        # Always: loop mode never EOFs, but a mini-pipeline ERROR must still end the run
        # (finished is set there too) instead of idling forever.
        return True

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
        return 1e9 / self._median_ns if self._median_ns else None

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self._run.header["width"]), int(self._run.header["height"]))

    def pixel_format(self) -> str:
        return "I420" if self._enc else (self._run.header.get("pixel_format") or "GRAY8")

    @property
    def encoded_caps(self):
        return self._enc[0] if self._enc else None

    @property
    def encoded_parser(self):
        return self._enc[1] if self._enc else None

    @property
    def tick_frequency_hz(self) -> int:
        return int(self._run.header.get("tick_frequency_hz") or 0)

    @property
    def ptp_locked(self) -> bool:
        return bool(self._run.header.get("ptp_synced"))

    @property
    def active_timestamp_source(self) -> str:
        return self._run.header.get("timestamp_source") or TimestampSource.SYSTEM.value
