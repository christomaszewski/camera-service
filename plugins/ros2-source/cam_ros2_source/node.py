"""ros2-source: subscribe to a ROS 2 image topic and FEED a camera-service instance through its shm
input (docs/TRANSPORT.md "Input"). The instance runs `camera: {type: shm}`; this node is the writer
that owns the socket, in the instance's socket volume.

  sensor_msgs/Image (or CompressedImage: jpeg / png)
      -> [convert pipeline, only when the message is not already the pinned format:
          appsrc ! (jpegdec|pngdec) ! videoconvert ! videoscale ! <pinned caps> ! appsink]
      -> one of three framings on the instance's socket (CAM_SOURCE_TRANSPORT = the core's shm.framing):
           raw     appsrc (pinned video/x-raw caps) ! shmsink          -- the core stamps on arrival
           header  36-byte application/x-cam-frame header + pixels ! shmsink   -- header.stamp travels
           unixfd  memfd buffers with native caps ! unixfdsink (GStreamer >= 1.24 on BOTH sides)
                   frame id in buffer.offset, capture ns in buffer.offset_end (the core's convention)

Everything is env-driven (sensor_env derives it from the instance's config, cam-up exports it):
  CAM_SOURCE_TOPIC        the image topic (absolute, e.g. /front/image_raw)      [required]
  CAM_SOURCE_TRANSPORT    raw | header | unixfd                                  [raw]
  CAM_SOURCE_SOCKET       the socket, in the instance's volume                   [/tmp/cam/in]
  CAM_SOURCE_FORMAT / _WIDTH / _HEIGHT / _FPS   the pinned shm: block             [RGB 640 480 10]
  CAM_SOURCE_COMPRESSED   true = the topic is sensor_msgs/CompressedImage         [false]
  CAM_SOURCE_QOS          sensor (best effort) | reliable                         [sensor]
  CAM_SOURCE_SHM_FRAMES   shmsink ring size, in frames                            [8]

The message's header.stamp is the frame's capture time (`camera` provenance); a zero stamp falls
back to arrival (`system`). Frame ids are minted here, monotonic. A message whose encoding is not
supported is dropped and logged once per encoding.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from . import framing as F  # noqa: E402

log = logging.getLogger("ros2-source")
TRANSPORTS = ("raw", "header", "unixfd")
STATS_EVERY_S = 10.0


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self, env=os.environ):
        self.topic = env.get("CAM_SOURCE_TOPIC", "").strip()
        if not self.topic:
            raise ValueError("CAM_SOURCE_TOPIC is required (the image topic to subscribe)")
        self.transport = env.get("CAM_SOURCE_TRANSPORT", "raw").strip().lower() or "raw"
        if self.transport not in TRANSPORTS:
            raise ValueError(f"CAM_SOURCE_TRANSPORT must be one of {TRANSPORTS}, got {self.transport!r}")
        self.socket = env.get("CAM_SOURCE_SOCKET", "/tmp/cam/in").strip() or "/tmp/cam/in"
        self.pinned = F.Pinned(env.get("CAM_SOURCE_FORMAT", "RGB"), int(env.get("CAM_SOURCE_WIDTH", "640") or 640),
                               int(env.get("CAM_SOURCE_HEIGHT", "480") or 480), float(env.get("CAM_SOURCE_FPS", "10") or 10))
        self.compressed = _truthy(env.get("CAM_SOURCE_COMPRESSED", "false"))
        self.qos = env.get("CAM_SOURCE_QOS", "sensor").strip().lower() or "sensor"
        self.shm_frames = max(2, int(env.get("CAM_SOURCE_SHM_FRAMES", "8") or 8))


class Feeder:
    """The GStreamer side: an output pipeline built once for the pinned format, a convert pipeline
    built lazily for the input caps actually seen (rebuilt if they change). Thread-safe pushes:
    the ROS executor pushes into appsrcs; GStreamer's streaming threads call back into _emit."""

    def __init__(self, s: Settings):
        self.s = s
        self.pinned = s.pinned
        self.out: Gst.Element | None = None
        self.out_pipeline: Gst.Pipeline | None = None
        self.cv_pipeline: Gst.Pipeline | None = None
        self.cv_in: Gst.Element | None = None
        self.cv_caps = ""
        self._fd_alloc = None
        self._GstAllocators = None
        self._pts_base = 0                       # absolute ns of pts 0 (relative PTS: no 56-year stall)
        self._emitted = 0
        self._converted = 0
        self._bypassed = 0
        self._dropped = 0
        self._unsupported: set[str] = set()
        self._lock = threading.Lock()
        self.failed: str | None = None           # a fatal output error (exit for a restart)
        self._last_stats = time.monotonic()
        self._first_logged = False

    # ---- output pipeline --------------------------------------------------------------------------
    def start(self) -> None:
        s, p = self.s, self.pinned
        if s.transport == "unixfd":
            if Gst.ElementFactory.find("unixfdsink") is None:
                raise RuntimeError(f"transport unixfd needs GStreamer >= 1.24 (unixfdsink); this image has {Gst.version_string()}")
            gi.require_version("GstAllocators", "1.0")
            from gi.repository import GstAllocators  # noqa: WPS433
            self._GstAllocators = GstAllocators
            self._fd_alloc = GstAllocators.FdAllocator.new()
            # unixfdsink will not rebind over a stale socket left by a killed writer
            try:
                os.unlink(s.socket)
            except FileNotFoundError:
                pass
            desc = (f'appsrc name=out is-live=true format=time do-timestamp=false caps="{p.unixfd_caps()}" '
                    f'! queue max-size-buffers=4 leaky=downstream ! unixfdsink socket-path="{s.socket}" sync=false')
        else:
            caps = F.HEADER_CAPS if s.transport == "header" else p.raw_caps()
            per_frame = p.frame_bytes + (F.HEADER_SIZE if s.transport == "header" else 0)
            shm_size = per_frame * s.shm_frames + 4096
            desc = (f'appsrc name=out is-live=true format=time do-timestamp=false caps="{caps}" '
                    f'! queue max-size-buffers=4 leaky=downstream '
                    f'! shmsink socket-path="{s.socket}" wait-for-connection=false shm-size={shm_size} sync=false')
        self.out_pipeline = Gst.parse_launch(desc)
        self.out = self.out_pipeline.get_by_name("out")
        bus = self.out_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_out_error)
        if self.out_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"output pipeline failed to start: {desc}")
        log.info("ros2-source: %s -> %s (%s framing) as %s %dx%d @ %g fps", s.topic, s.socket, s.transport,
                 p.pixel_format, p.width, p.height, p.fps)

    def _on_out_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        self.failed = f"output pipeline error: {err} ({(dbg or '').splitlines()[0][:160] if dbg else ''})"
        log.error("%s", self.failed)

    def stop(self) -> None:
        for pl in (self.cv_pipeline, self.out_pipeline):
            if pl is not None:
                pl.set_state(Gst.State.NULL)
        self.cv_pipeline = self.out_pipeline = None

    # ---- convert pipeline -------------------------------------------------------------------------
    def _ensure_convert(self, in_caps: str, decoder: str) -> None:
        if self.cv_pipeline is not None and self.cv_caps == in_caps:
            return
        if self.cv_pipeline is not None:
            log.info("ros2-source: input changed to %s; rebuilding the convert pipeline", in_caps)
            self.cv_pipeline.set_state(Gst.State.NULL)
        dec = f"{decoder} ! " if decoder else ""
        desc = (f'appsrc name=in is-live=true format=time do-timestamp=false caps="{in_caps}" '
                f"! {dec}videoconvert ! videoscale ! {self.pinned.raw_caps()} "
                f"! appsink name=cv emit-signals=true max-buffers=2 drop=true sync=false")
        self.cv_pipeline = Gst.parse_launch(desc)
        self.cv_in = self.cv_pipeline.get_by_name("in")
        self.cv_pipeline.get_by_name("cv").connect("new-sample", self._on_converted)
        bus = self.cv_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_cv_error)
        self.cv_caps = in_caps
        if self.cv_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError(f"convert pipeline failed to start: {desc}")
        log.info("ros2-source: converting %s -> %s", in_caps, self.pinned.raw_caps())

    def _on_cv_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        log.error("convert pipeline error: %s (%s) -- dropping until the input changes", err,
                  (dbg or "").splitlines()[0][:160] if dbg else "")
        self.cv_caps = ""    # the next message rebuilds it

    def _on_converted(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else 0
        self._emit(buf.extract_dup(0, buf.get_size()), self._pts_base + pts)
        self._converted += 1
        return Gst.FlowReturn.OK

    # ---- input -------------------------------------------------------------------------------------
    def _rel_pts(self, stamp: int) -> int:
        """Absolute ns -> a small relative PTS (an epoch-ns PTS stalls GStreamer flow)."""
        if self._pts_base == 0:
            self._pts_base = stamp
        if stamp < self._pts_base:      # a clock that jumped back: keep PTS monotonic-ish, keep the stamp
            self._pts_base = stamp
        return stamp - self._pts_base

    def push_image(self, encoding: str, width: int, height: int, is_bigendian: bool, step: int, data, stamp: int) -> None:
        if self.failed:
            return
        if stamp <= 0:
            stamp = time.time_ns()
        try:
            enc = F.Encoding(encoding, is_bigendian)
        except ValueError as e:
            if encoding not in self._unsupported:
                self._unsupported.add(encoding)
                log.error("ros2-source: %s -- dropping these", e)
            self._dropped += 1
            return
        if not self._first_logged:
            self._first_logged = True
            log.info("ros2-source: first frame: %s %dx%d step=%d, %s", encoding, width, height, step,
                     "already the pinned format" if self.pinned.matches(enc, width, height) else "converting")
        if self.pinned.matches(enc, width, height):
            row = enc.bytes_per_pixel * int(width) if enc.bytes_per_pixel else 0
            pixels = F.unpad_rows(data, step, row, height) if row else bytes(data)
            if len(pixels) != self.pinned.frame_bytes:
                self._drop_once(f"a {encoding} {width}x{height} message carries {len(pixels)} bytes, expected {self.pinned.frame_bytes}")
                return
            self._bypassed += 1
            self._emit(pixels, stamp)
            return
        # conversion: the message's own caps in, the pinned caps out; the stamp rides a relative PTS
        row = enc.bytes_per_pixel * int(width) if enc.bytes_per_pixel else 0
        pixels = F.unpad_rows(data, step, row, height) if row else bytes(data)
        self._ensure_convert(enc.caps(width, height, self.pinned.fps), "")
        self._push_convert(pixels, stamp)

    def push_compressed(self, fmt: str, data, stamp: int) -> None:
        if self.failed:
            return
        if stamp <= 0:
            stamp = time.time_ns()
        f = str(fmt or "").lower()
        if "png" in f:
            caps, decoder = "image/png", "pngdec"
        elif "jpeg" in f or "jpg" in f or f == "":
            caps, decoder = "image/jpeg", "jpegdec"
        else:
            self._drop_once(f"CompressedImage format {fmt!r} is not jpeg or png")
            return
        self._ensure_convert(caps, decoder)
        self._push_convert(bytes(data), stamp)

    def _push_convert(self, payload: bytes, stamp: int) -> None:
        buf = Gst.Buffer.new_wrapped(payload)
        buf.pts = self._rel_pts(stamp)
        buf.duration = Gst.CLOCK_TIME_NONE
        if self.cv_in.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            self._dropped += 1

    def _drop_once(self, why: str) -> None:
        self._dropped += 1
        if why not in self._unsupported:
            self._unsupported.add(why)
            log.error("ros2-source: %s -- dropping", why)

    # ---- output -----------------------------------------------------------------------------------
    def _emit(self, pixels: bytes, stamp: int) -> None:
        s, p = self.s, self.pinned
        with self._lock:
            self._emitted += 1
            frame_id = self._emitted
            source = "camera"
            if s.transport == "header":
                buf = Gst.Buffer.new_wrapped(F.pack_header(stamp, frame_id, p.width, p.height, p.format, source) + pixels)
            elif s.transport == "unixfd":
                fd = os.memfd_create("ros2-source", 0)
                try:
                    os.ftruncate(fd, len(pixels))
                    os.pwrite(fd, pixels, 0)
                    mem = self._GstAllocators.FdAllocator.alloc(self._fd_alloc, fd, len(pixels),
                                                                self._GstAllocators.FdMemoryFlags.NONE)
                except Exception:
                    os.close(fd)
                    raise
                buf = Gst.Buffer.new()
                buf.insert_memory(-1, mem)
                buf.offset = frame_id
                buf.offset_end = stamp
            else:
                buf = Gst.Buffer.new_wrapped(pixels)
            buf.pts = self._rel_pts(stamp)
            buf.duration = Gst.CLOCK_TIME_NONE
            ret = self.out.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                self._dropped += 1
                if ret != Gst.FlowReturn.FLUSHING:
                    log.warning("ros2-source: push to the output returned %s", ret)
        now = time.monotonic()
        if now - self._last_stats >= STATS_EVERY_S:
            self._last_stats = now
            log.info("ros2-source: %d frames out (%d as-is, %d converted), %d dropped, last stamp %d",
                     self._emitted, self._bypassed, self._converted, self._dropped, stamp)


def _qos(kind: str):
    from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
    if kind == "reliable":
        return QoSProfile(reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_LAST, depth=2)
    q = qos_profile_sensor_data
    q.depth = 1
    return q


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    Gst.init(None)
    try:
        s = Settings()
    except ValueError as e:
        log.error("%s", e)
        return 2
    feeder = Feeder(s)
    try:
        feeder.start()
    except RuntimeError as e:
        log.error("%s", e)
        return 3
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, name="gst-bus", daemon=True).start()

    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import CompressedImage, Image

    rclpy.init(args=argv)
    node = Node("cam_ros2_source")
    if s.compressed:
        node.create_subscription(CompressedImage, s.topic, lambda m: feeder.push_compressed(
            m.format, m.data, F.stamp_ns(m.header.stamp.sec, m.header.stamp.nanosec)), _qos(s.qos))
    else:
        node.create_subscription(Image, s.topic, lambda m: feeder.push_image(
            m.encoding, m.width, m.height, bool(m.is_bigendian), m.step, m.data,
            F.stamp_ns(m.header.stamp.sec, m.header.stamp.nanosec)), _qos(s.qos))
    log.info("ros2-source: subscribed to %s (%s, qos %s)", s.topic, "CompressedImage" if s.compressed else "Image", s.qos)
    rc = 0
    try:
        while rclpy.ok() and not feeder.failed:
            rclpy.spin_once(node, timeout_sec=0.5)
        if feeder.failed:
            rc = 4
    except KeyboardInterrupt:
        pass
    finally:
        feeder.stop()
        loop.quit()
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:   # noqa: BLE001
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
