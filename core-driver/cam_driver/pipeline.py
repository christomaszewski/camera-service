"""GStreamer pipeline: source-fed appsrc -> tee -> [recorder][raw endpoint][preview],
plus a separate transport appsrc carrying frames to out-of-process plugins.

The capture source (see cam_driver.sources) delivers (FrameStamp, image_bytes) per frame
to on_frame(), which is source-agnostic and:
  - sets the GstBuffer PTS = (timestamp - base) and OFFSET = frame_id,
  - pushes the raw video into the main appsrc (recorder / optional raw shm / preview),
  - and -- rate-limited -- pushes a copy into the transport appsrc for plugins.

The plugin transport endpoint has two implementations, picked at build() by capability:
  - JP7 (GStreamer >= 1.24, `unixfdsink` present): a `unixfdsink` carrying NATIVE caps
    (video/x-raw GRAY8/16 for mono, video/x-bayer,<pattern> for Bayer) + buffer fields
    over SCM_RIGHTS. No header -- the stream is self-describing. unixfdsink needs FD-backed
    memory, so the feeder copies each frame into a memfd (GstAllocators.FdAllocator); ~shm
    cost, the win is cleanliness. frame_id rides in buffer.offset, the absolute capture ns
    in buffer.offset_end (an absolute-ns PTS stalls downstream flow); PTS stays relative.
  - JP6 (GStreamer 1.20, no unixfd): a `shmsink` carrying a custom 36-byte
    `application/x-cam-frame` header (shm drops caps/PTS/meta, so we prepend our own).

unixfd REPLACES the header endpoint where available (not both); the raw headless shm sink
(raw_endpoint) is independent and config-gated on both platforms.

Why a separate transport appsrc rather than a post-tee transform: the transport branch needs
its own caps/allocation (header bytes on shm, memfd buffers on unixfd) that the tee can't
negotiate across branches. Building it in the feeder -- which already holds the absolute
timestamp + frame_id -- avoids a fragile post-tee buffer rewrite, and rate limiting becomes
a simple time check here.

NOTE (packed formats): assumes 8-bit or 16-bit-aligned data (Mono8 / Mono16 / Bayer*8).
Packed formats (Mono10p/Mono12Packed) need a bit-unpack step not implemented yet.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from . import recorder as rec
from . import transport
from .dropstats import DropStats
from .formats import bytes_per_frame, parse_pixel_format, select_encoder
from .sidecar import SidecarHeader, SidecarWriter
from .sources.base import SourceConfigChanged
from .timestamps import FrameStamp

log = logging.getLogger(__name__)

# appsrc queue bounds, in FRAMES (x frame size -> max-bytes). appsrc with block=false does NOT
# enforce its own bound -- past max-bytes it keeps queueing and push-buffer still returns OK -- so
# the feeder checks the fill level before each push (_queue_full) and drops WITH accounting,
# instead of growing RAM at sensor bandwidth until the OOM killer takes the recording with it.
_REC_QUEUE_FRAMES = 32   # recording feeds (camsrc/recsrc/encsrc): ride out brief stalls before dropping
_PUB_QUEUE_FRAMES = 8    # best-effort publish feeds (transport/unixfd): drop early, stay lean


class CapturePipeline:
    def __init__(self, cfg, source, sidecar: SidecarWriter):
        self.cfg = cfg
        self.source = source
        self.sidecar = sidecar
        self.drops = DropStats()
        self.pipeline: Optional[Gst.Pipeline] = None
        self.appsrc: Optional[Gst.Element] = None
        self.transport_src: Optional[Gst.Element] = None
        self.unixfd_src: Optional[Gst.Element] = None
        self.loop: Optional[GLib.MainLoop] = None
        self._base_ts: Optional[int] = None
        self._last_pub_ts: Optional[int] = None
        self._pub_err_last: float = 0.0             # monotonic s of the last logged publish error
        self._last_pts: Optional[int] = None        # for the monotonic-PTS guard
        self._frame_interval_ns = 1_000_000         # set in build() from frame_rate
        self._reconnecting = False
        self._stop_event = threading.Event()         # wakes the reconnect backoff on shutdown
        self._n_pushed = 0
        self._gst_format = "GRAY8"
        self._bits = 8
        self._bayer = None
        self._is_color = False
        self._width = 0
        self._height = 0
        self._image_size = 0
        self._stopping = False
        self._fatal = False                # pipeline ERROR / fatal source change -> non-zero exit
        self._have_unixfd = False          # JP7 (GStreamer 1.24): unixfd transport available
        self._unixfd_path = None
        self._fd_alloc = None              # GstAllocators.FdAllocator -> memfd buffers for unixfd
        self._GstAllocators = None
        self.rec_src: Optional[Gst.Element] = None   # private recorder appsrc when CFA-tiling is on
        self._tile_rec = False             # deinterleave the Bayer mosaic into quadrants for the recorder
        self._tile_mode = "off"            # off | plain | green_diff | rct (recording.bayer_tile)
        self._tiler = None                 # closure: frame_bytes -> tiled bytes (lazy; needs numpy)
        self.enc_src: Optional[Gst.Element] = None   # encoded appsrc for stream-copy recording
        self._enc_caps_applied = False     # set the encoded appsrc's real negotiated caps once (1st buffer)
        self._stream_copy = False          # encoded source -> recorder muxes the delivered bitstream verbatim
        self._base_lock = threading.Lock()  # base-ts init is shared by the raw + encoded callbacks

    # ---- build -------------------------------------------------------------
    @staticmethod
    def _shm_size(endpoint, unit_bytes: int) -> int:
        return endpoint.shm_size if endpoint.shm_size > 0 else max(unit_bytes * 8, 1 << 20)

    @staticmethod
    def _ensure_socket_dir(path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def build(self) -> str:
        Gst.init(None)
        _x, _y, width, height = self.source.geometry()
        pf = self.source.pixel_format()
        self._gst_format, self._bits, self._bayer, packed, self._is_color = parse_pixel_format(pf)
        self._width, self._height = int(width), int(height)
        if packed:
            log.warning("pixel format %s appears PACKED; fed as %s and will misinterpret data. "
                        "Use Mono8/Mono16/Bayer*8 or add an unpack step.", pf, self._gst_format)

        fps = self.cfg.camera.frame_rate
        framerate = f"{int(round(fps))}/1" if fps else "0/1"
        self._frame_interval_ns = int(1_000_000_000 / fps) if fps else 1_000_000
        caps = (f"video/x-raw,format={self._gst_format},width={self._width},"
                f"height={self._height},framerate={framerate}")
        self._image_size = bytes_per_frame(self._gst_format, self._width, self._height)
        frame_bytes = self._image_size

        main = [
            f'appsrc name=camsrc is-live=true do-timestamp=false format=time '
            f'max-bytes={_REC_QUEUE_FRAMES * frame_bytes} caps="{caps}"',
            "queue max-size-buffers=8 name=src_q",
            "tee name=t",
        ]
        branches = []
        is_encoded = self.source.encoded_caps is not None
        enc_parser = self.source.encoded_parser if is_encoded else None
        rec_desc = None
        if self.cfg.recording.enabled:
            loc = f"{self.cfg.recording.output_dir.rstrip('/')}/{self.cfg.recording.name_prefix}"
            rec_desc = rec.build_recorder_description(self.cfg.recording, self._bits, loc, fps,
                                                      self._is_color, enc_parser)
            self._stream_copy = (select_encoder(self.cfg.recording.encoder, self._bits,
                                                 self._is_color, encoded=is_encoded) == "stream-copy")
            if self._stream_copy:
                # Encoded source: the recorder stream-copies the delivered bitstream via a SEPARATE
                # encoded appsrc (encsrc, appended below) -- NOT a tee branch. The decoded frames still
                # flow to the consumer tee branches (transport/raw/preview), so live consumers are
                # unaffected; only the LOG bypasses decode/re-encode.
                log.info("recorder: stream-copy (%s) for encoded source", self.source.encoded_caps)
            else:
                # Raw recorder (raw source, or an encoded source the user forced to re-encode). CFA-tile
                # only an 8-bit Bayer mosaic, recorder feed only (the tee keeps the mosaic for the rest).
                if self._bayer and self._bits <= 8:
                    from . import bayer_tile
                    self._tile_mode = bayer_tile.normalize_mode(self.cfg.recording.bayer_tile)
                    if self._tile_mode == "off" and self.cfg.recording.bayer_tile not in (False, None, "", "off"):
                        log.warning("unknown recording.bayer_tile %r; recording the mosaic untiled",
                                    self.cfg.recording.bayer_tile)
                    if self._tile_mode != "off":
                        pat, mode = (self._bayer or "rggb"), self._tile_mode
                        self._tiler = lambda b: bayer_tile.tile_cfa(b, self._width, self._height, mode, pat)
                        self._tile_rec = True
                if not self._tile_rec:
                    branches.append("t. ! " + rec_desc)

        raw = self.cfg.transport.raw_endpoint
        if raw.enabled:
            self._ensure_socket_dir(raw.socket_path)
            shm = self._shm_size(raw, frame_bytes)
            branches.append(
                f"t. ! queue leaky=downstream max-size-buffers=4 ! "
                f"shmsink socket-path={raw.socket_path} shm-size={shm} "
                f"wait-for-connection=false sync=false")
            log.info("raw video endpoint -> %s (%d-byte shm)", raw.socket_path, shm)

        if self.cfg.preview.enabled:
            branches.append(f"t. ! queue leaky=downstream max-size-buffers=4 ! videoconvert ! {self.cfg.preview.sink}")
        else:
            branches.append("t. ! queue leaky=downstream max-size-buffers=4 ! fakesink sync=false")

        chains = [" ! ".join(main) + " " + " ".join(branches)]

        # CFA tiling: the recorder gets a private appsrc fed deinterleaved (quadrant-tiled) frames so its
        # lossless encoder sees smooth same-colour planes instead of the CFA checkerboard -- better spatial
        # AND temporal compression. numpy is imported lazily (only when tiling is actually enabled).
        if self._tile_rec:
            chains.append(
                f'appsrc name=recsrc is-live=true do-timestamp=false format=time '
                f'max-bytes={_REC_QUEUE_FRAMES * frame_bytes} caps="{caps}" '
                f'! {rec_desc}')
            log.info("recorder: CFA-tiling 8-bit Bayer (%s) mode=%s before encode", self._bayer, self._tile_mode)

        # Stream-copy recorder for an encoded source: a separate encoded appsrc feeds the delivered
        # bitstream (set by the feeder's on_encoded) straight into <parser> ! splitmuxsink -- no decode,
        # no re-encode, so the .mkv is byte-exact to what the host received.
        if self._stream_copy:
            # max-bytes uses the RAW frame size: encoded frames are (much) smaller, so the bound
            # is roomy in frames while still hard-capping memory.
            chains.append(
                f'appsrc name=encsrc is-live=true do-timestamp=false format=time '
                f'max-bytes={_REC_QUEUE_FRAMES * frame_bytes} '
                f'caps="{self.source.encoded_caps}" ! {rec_desc}')

        # Plugin transport endpoint: prefer unixfd (native caps + GstBuffer metadata) where the element
        # exists (JP7 / GStreamer 1.24), else the shm+header endpoint. unixfd REPLACES the header endpoint
        # (the raw headless shm above is separate + config-optional). unixfdsink needs FD-backed buffers,
        # so this is a SEPARATE appsrc fed memfd buffers by the feeder -- NOT a tee tap (the tee can't
        # negotiate the memfd allocation across branches). The plane rides as video/x-bayer,<pattern>
        # (8-bit Bayer) or video/x-raw GRAY8/16 (mono); offset=frame_id, offset_end=abs-ts (PTS stays relative).
        pe = self.cfg.transport.plugin_endpoint
        self._have_unixfd = bool(pe.enabled) and Gst.ElementFactory.find("unixfdsink") is not None
        if self._have_unixfd:
            gi.require_version("GstAllocators", "1.0")
            from gi.repository import GstAllocators
            self._GstAllocators = GstAllocators
            self._fd_alloc = GstAllocators.FdAllocator.new()
            self._unixfd_path = os.path.join(os.path.dirname(pe.socket_path) or "/tmp/cam", "unixfd")
            self._ensure_socket_dir(self._unixfd_path)
            # unixfdsink binds a fresh AF_UNIX socket and will NOT rebind over a stale one. A hard
            # restart (crash / container restart with a persistent socket volume) leaves the socket
            # file behind -> unixfdsink "Failed to start". Unlink any stale socket first. (shmsink
            # manages its own file, so the legacy endpoint doesn't need this.)
            try:
                if os.path.exists(self._unixfd_path):
                    os.unlink(self._unixfd_path)
            except OSError as e:
                log.warning("could not remove stale unixfd socket %s: %s", self._unixfd_path, e)
            if self._bayer and self._bits <= 8:
                ucaps = (f"video/x-bayer,format={self._bayer},width={self._width},"
                         f"height={self._height},framerate={framerate}")
            else:
                ucaps = caps   # mono: video/x-raw GRAY8/16
            chains.append(
                f'appsrc name=unixfd_src is-live=true do-timestamp=false format=time '
                f'max-bytes={_PUB_QUEUE_FRAMES * frame_bytes} caps="{ucaps}" '
                f'! queue max-size-buffers=8 ! unixfdsink socket-path={self._unixfd_path} sync=false')
            log.info("plugin transport endpoint (unixfd) -> %s  caps=%s", self._unixfd_path,
                     ucaps.split(",", 1)[0] + (f",{self._bayer}" if (self._bayer and self._bits <= 8) else ""))
        elif pe.enabled:
            self._ensure_socket_dir(pe.socket_path)
            shm = self._shm_size(pe, frame_bytes + transport.HEADER_SIZE)
            chains.append(
                f'appsrc name=transport_src is-live=true do-timestamp=false format=time '
                f'max-bytes={_PUB_QUEUE_FRAMES * (frame_bytes + transport.HEADER_SIZE)} '
                f'caps="{transport.CAPS}" ! queue max-size-buffers=8 ! '
                f"shmsink socket-path={pe.socket_path} shm-size={shm} "
                f"wait-for-connection=false sync=false")
            log.info("plugin transport endpoint (shm+header) -> %s (%d-byte shm, max_rate=%s)",
                     pe.socket_path, shm, pe.max_rate_hz or "unlimited")

        desc = "   ".join(chains)   # multiple top-level chains in one pipeline
        log.info("pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        self.appsrc = self.pipeline.get_by_name("camsrc")
        self.transport_src = self.pipeline.get_by_name("transport_src")
        self.unixfd_src = self.pipeline.get_by_name("unixfd_src")
        self.rec_src = self.pipeline.get_by_name("recsrc")
        self.enc_src = self.pipeline.get_by_name("encsrc")
        if self.appsrc is None:
            raise RuntimeError("appsrc 'camsrc' not found after parse_launch")
        return desc

    # ---- the timestamp-extracting feeder ----------------------------------
    def _should_publish(self, ts_ns: int) -> bool:
        rate = self.cfg.transport.plugin_endpoint.max_rate_hz
        if rate <= 0:
            return True
        min_interval = 1_000_000_000.0 / rate
        if self._last_pub_ts is None or (ts_ns - self._last_pub_ts) >= min_interval:
            self._last_pub_ts = ts_ns
            return True
        return False

    def _note_push_drop(self, publish: bool) -> int:
        if publish:
            self.drops.note_publish_drop()
            return self.drops.publish_drops
        self.drops.note_enqueue_failure()
        return self.drops.enqueue_failures

    def _queue_full(self, src, nbytes: int, what: str, publish: bool = False) -> bool:
        """True (recording the drop) if `src`'s internal queue can't take nbytes more. appsrc with
        block=false ignores its own max-bytes for queueing purposes (push still returns OK), so this
        check IS the bound: a full queue means downstream stalled -- drop this frame, count it, and
        keep the service alive instead of growing RAM until the OOM killer ends the recording."""
        max_bytes = src.get_property("max-bytes")
        if max_bytes and src.get_property("current-level-bytes") + nbytes > max_bytes:
            n = self._note_push_drop(publish)
            if n % 100 == 1:
                log.warning("%s queue full (downstream stalled); dropped %d frame(s) so far", what, n)
            return True
        return False

    def _push_checked(self, src, buf, what: str, publish: bool = False) -> bool:
        """push-buffer + FlowReturn check (flushing/EOS race), with the same drop accounting."""
        ret = src.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            self._note_push_drop(publish)
            log.warning("%s push-buffer -> %s (frame received but not enqueued)", what, ret)
            return False
        return True

    def _ensure_base(self, stamp: FrameStamp) -> None:
        """Set the PTS base + write the sidecar header once, on the first frame from EITHER the raw
        (on_frame) or encoded (on_encoded) callback. They share the per-frame stamp, so the base is
        the same regardless of which fires first; locked because the two run on separate threads."""
        with self._base_lock:
            if self._base_ts is None:
                self._base_ts = stamp.timestamp_ns
                self._write_header()

    def _account(self, stamp: FrameStamp, pts: int, recorded: bool = True) -> None:
        """Per-RECORDED-frame accounting: frame-id drop detection + the sidecar timestamp row
        (+ a first-frames provenance eyeball). Driven by whichever callback feeds the recording:
        _on_frame for raw / re-encode sources, _on_encoded for a stream-copy source. Deliberately
        NOT the best-effort decode branch of a stream-copy source -- a leaky decode drop there is
        expected (not a lost recorded frame) and must not desync the sidecar or trip drop warnings.
        A frame whose recording push was dropped (recorded=False) is still observed for gap
        accounting but gets NO sidecar row, keeping the CSV 1:1 with the .mkv."""
        gap = self.drops.observe_frame(stamp.frame_id)
        if gap:
            log.warning("frame-id gap: %d frame(s) lost before fid=%s (source/link drop; %d missing total)",
                        gap, stamp.frame_id, self.drops.frames_missing)
        if recorded:
            self.sidecar.add(stamp, pts)
        if self._n_pushed < 5:  # quick eyeball; full per-frame data is in the CSV
            d_cc = (stamp.chunk_ns - stamp.camera_ns) if stamp.chunk_ns is not None else None
            d_sc = (stamp.system_ns - stamp.chunk_ns) if stamp.chunk_ns is not None else None
            log.info("ts[fid=%s] src=%s chunk=%s camera=%s system=%s  chunk-camera=%s system-chunk=%s",
                     stamp.frame_id, stamp.source.value, stamp.chunk_ns, stamp.camera_ns,
                     stamp.system_ns, d_cc, d_sc)
        self._n_pushed += 1

    def _on_frame(self, stamp: FrameStamp, frame_bytes: bytes) -> None:
        """Source callback (runs on the source's feeder thread): a resolved timestamp +
        clean image bytes. Set PTS/offset, push to the main appsrc (tee -> record/raw/
        preview), and -- rate-limited -- to the plugin transport endpoint. Source-agnostic.

        For a stream-copy (encoded) source this is the BEST-EFFORT decode branch: it feeds live
        consumers only (the recording rides the encoded branch -> _on_encoded). So the per-recorded-
        frame accounting (_account) runs here ONLY when this path feeds the recording (raw / re-encode);
        for stream-copy it lives in _on_encoded, so leaky decode drops never desync the sidecar."""
        if self._stopping:
            return   # draining for EOS; stop feeding the pipeline
        self._ensure_base(stamp)
        pts = stamp.timestamp_ns - self._base_ts
        if self._last_pts is not None and pts <= self._last_pts:
            # Non-monotonic timestamp (e.g. the camera clock reset across a reconnect).
            # Rebase so the muxer keeps a strictly-increasing PTS; the true timestamp is
            # still recorded per-frame in the sidecar CSV, so absolute time is recoverable.
            self._base_ts = stamp.timestamp_ns - (self._last_pts + self._frame_interval_ns)
            pts = self._last_pts + self._frame_interval_ns
            log.warning("timestamp discontinuity (ts went backward); rebased PTS to stay monotonic")
        if pts < 0:
            pts = 0
        self._last_pts = pts

        rec_ok = not self._queue_full(self.appsrc, len(frame_bytes), "camsrc")
        if rec_ok:
            gbuf = Gst.Buffer.new_wrapped(frame_bytes)
            gbuf.pts = pts
            gbuf.dts = Gst.CLOCK_TIME_NONE
            gbuf.offset = stamp.frame_id
            rec_ok = self._push_checked(self.appsrc, gbuf, "camsrc")

        # Recorder gets a CFA-tiled copy (quadrant sub-planes) for better lossless compression; the
        # tee above keeps feeding the mosaic to transport/preview/raw. Same PTS/frame_id. When
        # tiling is on, THIS push (not camsrc) is the recording feed.
        if self.rec_src is not None:
            tiled = self._tiler(frame_bytes)
            rec_ok = not self._queue_full(self.rec_src, len(tiled), "recsrc")
            if rec_ok:
                rbuf = Gst.Buffer.new_wrapped(tiled)
                rbuf.pts = pts
                rbuf.dts = Gst.CLOCK_TIME_NONE
                rbuf.offset = stamp.frame_id
                rec_ok = self._push_checked(self.rec_src, rbuf, "recsrc")

        # plugin transport endpoint, rate-limited. JP7 (unixfd): native caps + buffer fields, but
        # unixfdsink needs FD-backed memory -> copy the frame into a fresh memfd (~shm cost; the win
        # is a header-free, self-describing stream). Carry frame_id in .offset and the absolute PTP
        # capture time in .offset_end (an absolute-ns PTS would stall downstream flow). PTS stays
        # relative. JP6 (no unixfd): the legacy shm+header endpoint.
        if self._should_publish(stamp.timestamp_ns):
            try:
                if self.unixfd_src is not None:
                    # bound check BEFORE the memfd: no point paying the copy for a frame we then drop
                    if not self._queue_full(self.unixfd_src, len(frame_bytes), "unixfd transport", publish=True):
                        fd = os.memfd_create("cam", 0)
                        try:
                            os.ftruncate(fd, len(frame_bytes))
                            os.pwrite(fd, frame_bytes, 0)
                            mem = self._GstAllocators.FdAllocator.alloc(
                                self._fd_alloc, fd, len(frame_bytes),
                                self._GstAllocators.FdMemoryFlags.NONE)   # on success the allocator owns/closes the fd
                        except Exception:
                            os.close(fd)   # ownership never transferred; the handler below would otherwise
                            raise          # silently leak one fd per published frame until EMFILE
                        ubuf = Gst.Buffer.new()
                        ubuf.insert_memory(-1, mem)
                        ubuf.pts = pts
                        ubuf.offset = stamp.frame_id
                        ubuf.offset_end = stamp.timestamp_ns
                        self._push_checked(self.unixfd_src, ubuf, "unixfd transport", publish=True)
                elif self.transport_src is not None:
                    hdr = transport.FrameHeader(
                        timestamp_ns=stamp.timestamp_ns, frame_id=stamp.frame_id,
                        width=self._width, height=self._height,
                        pixfmt=self._gst_format, ts_source=stamp.source.value).pack()
                    payload = hdr + frame_bytes
                    if not self._queue_full(self.transport_src, len(payload), "shm transport", publish=True):
                        tbuf = Gst.Buffer.new_wrapped(payload)
                        tbuf.pts = pts
                        tbuf.offset = stamp.frame_id
                        self._push_checked(self.transport_src, tbuf, "shm transport", publish=True)
            except Exception as e:
                # The plugin endpoint is best-effort: a per-frame publish failure (e.g. a
                # TransportError for a pixel format the header can't carry, or a memfd
                # failure) must never escape past the _account below -- that would kill
                # drop detection and leave the sidecar empty while the recording continues.
                self.drops.note_publish_drop()
                now = time.monotonic()
                if now - self._pub_err_last >= 5.0:
                    self._pub_err_last = now
                    log.warning("plugin transport publish failed (throttled 5s): %s", e)

        if not self._stream_copy:
            self._account(stamp, pts, recorded=rec_ok)   # raw / re-encode: THIS path is the recording feed

    def _on_encoded(self, stamp: FrameStamp, enc_bytes: bytes, caps_str: str = None) -> None:
        """Encoded-source callback (parallel to on_frame, SAME per-frame stamp): push the delivered
        bitstream to the stream-copy recorder's appsrc with a stamp-derived PTS, so the .mkv aligns
        with the sidecar + the raw consumer path. No decode/re-encode -- byte-exact to delivery."""
        if self._stopping or self.enc_src is None:
            return
        # Apply the source's NEGOTIATED encoded caps once (carries stream-format + codec_data -- e.g.
        # the H.264/H.265 VPS/SPS/PPS that hvc1/avc keep in caps, not in the bytes) so the appsrc ->
        # h26xparse -> matroskamux chain negotiates. The build-time caps were the bare media type
        # ("video/x-h265"), enough for MJPEG (no codec_data) but not for H.264/H.265.
        if caps_str and not self._enc_caps_applied:
            self.enc_src.set_property("caps", Gst.Caps.from_string(caps_str))
            self._enc_caps_applied = True
        self._ensure_base(stamp)
        pts = stamp.timestamp_ns - self._base_ts
        if pts < 0:
            pts = 0
        rec_ok = not self._queue_full(self.enc_src, len(enc_bytes), "encsrc")
        if rec_ok:
            ebuf = Gst.Buffer.new_wrapped(enc_bytes)
            ebuf.pts = pts
            ebuf.dts = Gst.CLOCK_TIME_NONE
            ebuf.offset = stamp.frame_id
            rec_ok = self._push_checked(self.enc_src, ebuf, "encsrc")
        # stream-copy: the encoded branch IS the recording -> account the recorded frame here (drop
        # detection + sidecar timestamp), NOT on the best-effort decode branch (_on_frame). This keeps
        # the sidecar 1:1 with the .mkv and the RTCP->NTP provenance complete even under decode-branch load.
        self._account(stamp, pts, recorded=rec_ok)

    def _write_header(self) -> None:
        _x, _y, width, height = self.source.geometry()
        self.sidecar.write_header(SidecarHeader(
            created_unix_s=time.time(),
            base_timestamp_ns=int(self._base_ts),
            timestamp_source=self.source.active_timestamp_source,
            ptp_synced=self.source.ptp_locked,
            pixel_format=self.source.pixel_format(),
            bayer_pattern=self.cfg.recording.bayer_pattern or self._bayer,
            bits_per_pixel=self._bits,
            width=int(width),
            height=int(height),
            tick_frequency_hz=self.source.tick_frequency_hz,
            cfa_tile_mode=self._tile_mode,
        ))

    # ---- shutdown ----------------------------------------------------------
    def request_stop(self) -> None:
        """Clean stop: halt acquisition and inject EOS so the muxer finalizes the file.
        The bus EOS handler then quits the loop; a timer is the safety net."""
        if self._stopping:
            return
        self._stopping = True
        self._stop_event.set()   # wake the reconnect backoff, if one is in progress
        log.info("stop requested: stopping acquisition + sending EOS to finalize recording")
        self.source.stop()
        for src in (self.appsrc, self.transport_src, self.unixfd_src, self.rec_src, self.enc_src):
            if src is not None:
                src.emit("end-of-stream")
        GLib.timeout_add_seconds(5, self._force_quit)

    def _force_quit(self) -> bool:
        log.warning("EOS did not drain within 5s; forcing stop (recording may be truncated)")
        if self.loop and self.loop.is_running():
            self.loop.quit()
        return False

    def _request_stop_idle(self) -> bool:
        self.request_stop()
        return False   # one-shot idle callback

    @property
    def had_error(self) -> bool:
        """True if the run ended on a pipeline ERROR or a fatal source change (drives the exit code)."""
        return self._fatal

    # ---- run loop ----------------------------------------------------------
    def run(self) -> None:
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        self.loop = GLib.MainLoop()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.source.start(self._on_frame, self._on_encoded)
        if self.source.reconnect_enabled:
            GLib.timeout_add_seconds(1, self._watchdog)
        GLib.timeout_add_seconds(30, self._log_health)
        log.info("running")
        try:
            self.loop.run()
        finally:
            self.shutdown()

    def _log_health(self) -> bool:
        """~every 30s: surface drop accounting as a first-class live signal (not just at shutdown)."""
        if self._stopping:
            return False
        s = self.drops.summary()
        if s["source_gaps"] or s["enqueue_failures"] or s["publish_drops"]:
            log.warning("health: frames=%(frames)d source_gaps=%(source_gaps)d "
                        "frames_missing=%(frames_missing)d enqueue_failures=%(enqueue_failures)d "
                        "publish_drops=%(publish_drops)d", s)
        else:
            log.info("health: frames=%(frames)d, no drops", s)
        return True

    def _watchdog(self) -> bool:
        """Runs on the main loop ~1 Hz: ask the source whether it's disconnected and, if so,
        kick off a reconnect in its own thread (so backoff doesn't block the pipeline)."""
        if self._stopping:
            return False   # remove the watchdog
        if self._reconnecting:
            return True
        if self.source.is_disconnected():
            log.warning("source reports disconnect -> reconnecting")
            self._reconnecting = True
            threading.Thread(target=self._reconnect, name="reconnect", daemon=True).start()
        return True

    def _reconnect(self) -> None:
        """Backoff loop (own thread): re-open the source until it returns, then re-arm the
        feeder. The GStreamer pipeline stays PLAYING throughout -- the appsrc just idles, so
        the recording isn't finalized and consumers keep their shm connection."""
        self.source.stop()   # best-effort: drop the dead acquisition
        backoff = self.cfg.camera.reconnect_backoff_s
        attempt = 0
        while not self._stopping:
            attempt += 1
            try:
                self.source.reopen()
            except SourceConfigChanged as e:
                # The stream came back DIFFERENT (codec/geometry). The appsrc caps are fixed at
                # build(), so no in-process reopen can carry on: finalize the recording cleanly and
                # exit non-zero; the supervisor/compose restart re-probes and rebuilds for the new
                # format.
                log.error("%s -- finalizing recording and exiting for a clean rebuild", e)
                self._fatal = True
                self._reconnecting = False
                GLib.idle_add(self._request_stop_idle)
                return
            except Exception as e:   # noqa: BLE001 - never let the reconnect thread die
                log.warning("reconnect attempt %d failed: %s (retry in %.1fs)", attempt, e, backoff)
                if self._stop_event.wait(backoff):
                    break            # stop requested mid-backoff
                backoff = min(backoff * 2, self.cfg.camera.reconnect_backoff_max_s)
                continue
            self.source.start(self._on_frame, self._on_encoded)
            log.info("source reconnected after %d attempt(s); resuming capture", attempt)
            self._reconnecting = False
            return
        self._reconnecting = False

    def _on_bus(self, _bus, msg) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("GStreamer ERROR: %s | %s", err, dbg)
            self._fatal = True   # surfaced as a non-zero exit (disk full etc. must not look clean)
            if self.loop:
                self.loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            log.info("EOS")
            if self.loop:
                self.loop.quit()

    def shutdown(self) -> None:
        log.info("shutting down (pushed %d frames)", self._n_pushed)
        if not self._stopping:   # error/EOS path that didn't go through request_stop
            self.source.stop()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        s = self.drops.summary()
        log.info("drop summary: frames=%(frames)d source_gaps=%(source_gaps)d "
                 "frames_missing=%(frames_missing)d enqueue_failures=%(enqueue_failures)d "
                 "publish_drops=%(publish_drops)d", s)
        self.sidecar.write_summary(s)
        self.sidecar.stop()
