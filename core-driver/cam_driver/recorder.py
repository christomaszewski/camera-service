"""Pluggable recorder branch, lossless by default, built as a GStreamer launch fragment.

Explicit ``x264`` selects lossy 8-bit H.264/4:2:0 with configurable CRF and speed preset.
Higher-depth input retains FFV1; auto selection never chooses a lossy encoder.

Encoder selection (``auto``, see formats.select_encoder):
  * 8-bit mono/Bayer -> hardware HEVC lossless (NVENC, NV24/YUV444; bit-exact, temporal).
                    The mosaic rides in the Y plane (chroma neutral), recovered by dropping
                    chroma in post.
  * color (YUV/RGB)  -> FFV1 -- avoids the NV24 (4:4:4) conversion, which would resample a
                    4:2:0 source and break bit-exactness. (NVENC color-lossless fed the
                    native subsampling is a hardware refinement.)
  * >8-bit input     -> FFV1 (lossless, high-bit-depth; INTRA-only). x265-lossless is 8-bit-only:
                    >8-bit rides in a GRAY16 container that x265's <=12-bit input formats can't
                    carry, so a >8-bit request falls back to FFV1 instead of silently dropping bits.

Encoders degrade by AVAILABILITY too: the launch-fragment elements are probed at build time
(Gst.ElementFactory.find, mirroring the unixfdsink / HW-decoder capability checks) and a missing
encoder -- NVENC off-Jetson or in a container without the L4T stack, x265/libav not installed --
falls back to FFV1 with a warning instead of killing the service inside Gst.parse_launch.

Bit-exactness needs more than a lossless codec: the mono/Bayer plane must also cross the
GRAY8 -> NV24 (or -> I420) conversion verbatim. Those caps pin formats.LUMA_COLORIMETRY (full
range). Unpinned, GStreamer gives the YUV side its height-based default, which is limited range above
576 rows, and every recording taller than that lost up to 3 codes per pixel before NVENC/x265
saw it. The JetPack 7.2 validation (60/60 noise frames exact, 1.32x raw) ran at 512x512 and so
never reached that default. With the pin, an R39 Orin passes 24/24 noise frames at 2448x2048 (1.317x
raw) where the unpinned path passes 0/24; JP6 is not yet validated. Validate at the sensor geometry:
`tools/nvenc_lossless_test.sh` (or .py) with the NVENC CDI device, which defaults to 2448x2048.
Re-verify after a JetPack bump (NVMM caps negotiation and the lossless enum are L4T-version-dependent).

The hardware path's CPU cost is not the codec but getting the frame to it: `nvvidconv` copies the
whole frame into encoder memory at about 1.6 ms per MB, and NV24 is three bytes per pixel. Measured
on an R39 AGX Orin at 2448x2048 (docs/encoding-optimization/RESULTS.md): 24 ms of a 52 ms frame in
that copy, 12 ms in `videoconvert` building the NV24 frame. So the recorder can be FED NV12 instead
(`recording.nvenc_feed`): the frame itself plus one shared block of constant half-resolution chroma,
which the VIC upsamples to NV24 in hardware with the luma plane untouched -- 25 ms a frame, the same
bitstream. Which input formats the VIC passes through verbatim is its own business (NV12 does on
R39; I420 and GRAY8 do not), so the feed is PROVEN before it is used: `probe_nv12_feed` pushes
frames holding every 8-bit code through the real fragment at the real geometry and compares.
"""
from __future__ import annotations

import logging

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

import os
from typing import Optional

from .formats import LUMA_COLORIMETRY, select_encoder

log = logging.getLogger(__name__)

# recording.nvenc_feed: how an 8-bit mono/Bayer frame reaches the hardware lossless encoder.
#   auto         NV12 from the feeder when probe_nv12_feed proves it bit-exact on this host at this
#                geometry; otherwise the videoconvert feed
#   nv12         NV12 without the proof (a host where it is already qualified and the ~1 s is unwanted)
#   videoconvert the feed every release before this one used: GRAY8 -> videoconvert -> NV24
LUMA_FEEDS = ("auto", "nv12", "videoconvert")


def normalize_feed(value) -> str:
    v = str(value if value is not None else "auto").strip().lower()
    if v in LUMA_FEEDS:
        return v
    log.warning("recording.nvenc_feed %r is not one of %s; using auto", value, "/".join(LUMA_FEEDS))
    return "auto"


def nv12_geometry_ok(width: int, height: int) -> bool:
    """The feeder builds NV12 as two tight blocks (rows of `width`, then width x height/2 of chroma).
    That is GStreamer's own layout only when rows need no padding and the height is even."""
    return width > 0 and height > 0 and width % 4 == 0 and height % 2 == 0


def nv12_feed_caps(width: int, height: int, framerate: str) -> str:
    # Full range, like the NV24 the videoconvert feed builds: the plane is data, not a picture.
    return (f"video/x-raw,format=NV12,width={int(width)},height={int(height)},"
            f"framerate={framerate},colorimetry={LUMA_COLORIMETRY}")


class ChromaBlock:
    """The constant chroma half of an NV12 frame, shared by every buffer of a recording.

    Held as a GstBuffer and handed out as regions of it: copy_region(MEMORY) refs the memory instead of
    copying it, so each recorded frame gets the same 128s for the price of a small buffer struct. Two
    routes that look shorter are traps. Gst.Memory.new_wrapped does not copy the Python bytes it is
    given (a temporary dangles: the encoder then reads freed memory), and on the PyGObject of the 26.04
    images the call aborts the process outright. Gst.Buffer.new_wrapped copies into memory GLib owns,
    on every runtime the service has."""

    def __init__(self, width: int, height: int):
        self.size = int(width) * int(height) // 2
        self._buffer = Gst.Buffer.new_wrapped(bytes([128]) * self.size)

    def tail(self, buf):
        """Append the block to a buffer holding one luma plane: the result is a whole NV12 frame in two
        memories. Returns the buffer to use from here on (timestamps and metas of `buf` are kept)."""
        return buf.append(self._buffer.copy_region(Gst.BufferCopyFlags.MEMORY, 0, self.size))


# Encoders that carry a mono/Bayer plane in the Y plane of a YUV container.
_LUMA_IN_YUV = ("hw-hevc-lossless", "x265-lossless")


def luma_colorimetry(enc, is_color):
    """The colorimetry pinned on the YUV container a mono/Bayer recording rides in, for the sidecar.
    None when the plane never crosses a YUV conversion (ffv1, stream-copy, x264) or the source is
    colour (its own colorimetry carries through)."""
    return LUMA_COLORIMETRY if enc in _LUMA_IN_YUV and not is_color else None


# nvv4l2h265enc preset-level enum (HW search depth: bigger = smaller lossless file, slower encode).
_PRESET_LEVEL = {"disable": 0, "ultrafast": 1, "fast": 2, "medium": 3, "slow": 4}
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow", "placebo")


def x264_settings(cfg):
    """Validate before constructing a launch string; return settings also stored in the sidecar."""
    crf = getattr(cfg, "x264_crf", 23)
    if isinstance(crf, bool) or not isinstance(crf, int) or not 1 <= crf <= 50:
        raise ValueError("recording.x264_crf must be an integer from 1 to 50")
    preset = getattr(cfg, "x264_preset", "ultrafast")
    if preset not in X264_PRESETS:
        raise ValueError(f"recording.x264_preset must be one of {', '.join(X264_PRESETS)}")
    return {"crf": crf, "preset": preset, "chroma": "4:2:0"}


def _preset_level(value):
    """Map an nvenc preset name/number to its preset-level int, or None to leave the encoder default."""
    s = str(value or "").strip().lower()
    if s in _PRESET_LEVEL:
        return _PRESET_LEVEL[s]
    if s.isdigit() and 0 <= int(s) <= 4:
        return int(s)
    return None


# Encoder -> the launch-fragment elements that must exist on this host. The NVENC path needs the
# L4T stack (nvvidconv + nvv4l2h265enc: Jetson-only, and absent in a container that doesn't expose
# it); x265enc / avenc_ffv1 ride in optional codec packages. Probed at build time so a missing
# element degrades to a software fallback with a warning -- not a GLib.Error out of
# Gst.parse_launch that takes the whole service down.
_ENCODER_ELEMENTS = {
    "hw-hevc-lossless": ("nvvidconv", "nvv4l2h265enc"),
    "x265-lossless": ("x265enc",),
    "ffv1": ("avenc_ffv1",),
    "x264": ("x264enc", "h264parse"),
}


def _resolve_available(enc: str) -> str:
    """Degrade `enc` to an encoder whose GStreamer elements exist: hw-hevc-lossless /
    x265-lossless -> ffv1 (lossless everywhere, CPU). No avenc_ffv1 either -> RuntimeError:
    nothing lossless is left to record with, and one legible error beats a parse_launch crash."""
    missing = [e for e in _ENCODER_ELEMENTS.get(enc, ()) if Gst.ElementFactory.find(e) is None]
    if not missing:
        return enc
    if enc == "ffv1":
        raise RuntimeError(
            "recorder: no usable lossless encoder: ffv1 needs avenc_ffv1 "
            f"(gstreamer1.0-libav); missing element(s): {', '.join(missing)}")
    log.warning("recorder: %s unavailable on this host (missing GStreamer element(s): %s); "
                "falling back to ffv1", enc, ", ".join(missing))
    return _resolve_available("ffv1")


def _splitmux(location_base: str, seconds: int, muxer: str = "matroskamux",
              keyframe_requests: bool = True) -> str:
    # splitmuxsink preserves continuous PTS across segments (good for alignment). send-keyframe-requests
    # makes it ask the ENCODER (via an upstream force-key-unit event) for a keyframe at each split, so
    # every .mkv starts on a keyframe even with a long GOP. Stream-copy has NO encoder upstream to honor
    # that request -- it's just a parser + appsrc -- so the request is a no-op; we disable it and let
    # splitmuxsink split on the stream's existing keyframes (every MJPEG frame is intra; H.264/H.265 on IDRs).
    max_ns = max(1, int(seconds)) * Gst.SECOND
    kr = "send-keyframe-requests=true " if keyframe_requests else ""
    return (f'splitmuxsink name=rec_sink muxer={muxer} {kr}'
            f'location="{location_base}-%05d.mkv" max-size-time={max_ns}')


def _gop_frames(cfg, fps: float) -> int:
    """Keyframe interval in frames from the configured seconds-window (0 -> 0 = encoder default)."""
    if cfg.keyframe_interval_s and cfg.keyframe_interval_s > 0:
        return max(1, int(round(cfg.keyframe_interval_s * (fps if fps and fps > 0 else 30.0))))
    return 0


def _hw_hevc_lossless(cfg, gop: int, bframes: int) -> str:
    """NV24 or NV12 in system memory -> NVMM NV24 -> NVENC lossless -> parsed HEVC. One definition, so
    the recorder and the proof of its feed (probe_nv12_feed) cannot drift apart. iframeinterval = the
    GOP/keyframe window; preset-level = HW search depth; num-B-Frames only when asked (Xavier-only on
    Tegra). Property names and the lossless interplay are L4T-version-dependent."""
    opts = f" maxperf-enable={1 if getattr(cfg, 'nvenc_maxperf', True) else 0}"
    level = _preset_level(getattr(cfg, "nvenc_preset", ""))
    if level is not None:
        opts += f" preset-level={level}"
    if gop:
        opts += f" iframeinterval={gop}"
    if bframes:
        opts += f" num-B-Frames={bframes}"
    return ("nvvidconv ! video/x-raw(memory:NVMM),format=NV24 ! "
            f"nvv4l2h265enc enable-lossless=1{opts} ! h265parse")


def build_recorder_description(cfg, bits_per_pixel: int, location_base: str, fps: float = 0.0,
                               is_color: bool = False, encoded_parser: str = None,
                               luma_feed: str = "videoconvert"):
    """Return `(gst-launch fragment, resolved encoder)`. The fragment begins with a sink pad
    (linkable from `tee.` or an appsrc); encoded_parser set => the source is already-encoded:
    STREAM-COPY through that parser (no decode/re-encode) -- the faithful path for MJPEG/H.264/H.265.

    luma_feed="nv12": the caller feeds a mono/Bayer hw-hevc-lossless recording NV12 (nv12_feed_caps,
    ChromaBlock) instead of the raw plane, so that fragment starts at `nvvidconv`. It has no effect on
    any other encoder or on colour: check the returned encoder before feeding NV12.

    The encoder is returned because the CALLER has to wire the recorder differently for stream-copy
    (its own encoded appsrc) than for a re-encode (a tee branch), and re-deriving that from
    select_encoder alone misses every degrade below. pipeline.build() used to do exactly that, so
    `encoder: stream-copy` on a RAW source detached the recorder from the tee and hung ffv1 off an
    appsrc nothing ever fed -- a config that recorded nothing at all, silently."""
    enc = select_encoder(cfg.encoder, bits_per_pixel, is_color, encoded=bool(encoded_parser))
    if enc == "x264":
        x264_settings(cfg)
    sink = _splitmux(location_base, cfg.segment_seconds)

    if enc == "stream-copy":
        if not encoded_parser:
            log.warning("stream-copy requested but source isn't encoded; falling back to ffv1")
            enc = "ffv1"
        else:
            log.info("recorder: stream-copy (%s) -> %s-*.mkv (delivered bitstream, no re-encode)",
                     encoded_parser, location_base)
            sc_sink = _splitmux(location_base, cfg.segment_seconds, keyframe_requests=False)
            return f"queue max-size-buffers=12 name=rec_q ! {encoded_parser} ! " + sc_sink, enc

    if enc in ("x265-lossless", "hw-hevc-lossless", "x264") and bits_per_pixel > 8:
        # >8-bit rides in a GRAY16 container and NEITHER lossless path can carry it: x265's input
        # formats top out at 12-bit, and the Orin's HW lossless is 8-bit only (there is no 10/12-bit
        # NVENC lossless) -- videoconvert happily negotiates GRAY16_LE -> NV24 and keeps only the
        # HIGH byte, so a pinned `hw-hevc-lossless` on a 16-bit radiometric camera would throw away
        # the sensor's low 8 bits while the sidecar still attests bits_per_pixel: 16. FFV1 keeps the
        # 16-bit container bit-exact. The guard covers both encoders because they share the
        # constraint; the explicit x264 mode also writes 8-bit 4:2:0 and must not truncate thermal data.
        log.warning("recorder: %s cannot preserve >8-bit (GRAY16) input; falling back to ffv1", enc)
        enc = "ffv1"
    enc = _resolve_available(enc)

    gop = _gop_frames(cfg, fps)
    bframes = max(0, int(getattr(cfg, "bframes", 0)))
    preset = _preset_level(getattr(cfg, "nvenc_preset", "")) if enc == "hw-hevc-lossless" else None
    # videoconvert_threads spreads a REAL colour conversion over cores (n-threads=0 = all of them).
    # A mono/Bayer plane into its full-range YUV container is no longer one: with the pin it is a
    # copy, and on an Orin four threads then cost 10 ms more CPU per 5 MP frame than one, at the same
    # throughput (the encoder limits it). That copy runs on one thread whatever the knob says.
    nt = max(0, int(getattr(cfg, "videoconvert_threads", 4)))
    luma = luma_colorimetry(enc, is_color)
    nv12 = luma_feed == "nv12" and enc == "hw-hevc-lossless" and luma is not None
    if luma is not None:
        nt = 1
    vconv = f"videoconvert n-threads={nt}"
    log.info("recorder: encoder=%s (bits=%d) gop=%s bframes=%d preset=%s %s -> %s-*.mkv",
             enc, bits_per_pixel, gop or "default", bframes, "default" if preset is None else preset,
             "feed=nv12" if nv12 else f"vconv-threads={nt}", location_base)

    if enc == "x264":
        settings = x264_settings(cfg)
        # Bounded live recorder queues must not wait for x264 lookahead. P-only also keeps
        # decode order equal to presentation order; splitmuxsink requests IDRs at each split.
        # Disable VBV: x264enc otherwise caps even CRF at its default 2048 kbit/s,
        # silently reducing quality on detailed scenes regardless of the configured CRF.
        if bframes:
            log.warning("recorder: x264 ignores bframes; using P-only encoding for bounded latency")
        log.info("recorder: lossy H.264 CRF=%d preset=%s, 8-bit 4:2:0", settings["crf"], settings["preset"])
        return (
            f"queue max-size-buffers=12 name=rec_q ! {vconv} ! video/x-raw,format=I420 ! "
            f'x264enc pass=qual quantizer={settings["crf"]} speed-preset={settings["preset"]} '
            f"tune=zerolatency bframes=0 rc-lookahead=0 sync-lookahead=0 vbv-buf-capacity=0 key-int-max={gop} ! "
            "video/x-h264,profile=high ! h264parse ! " + sink
        ), enc

    if enc == "hw-hevc-lossless":
        # GRAY8 / Bayer8 mosaic -> Y plane of NV24 -> NVMM -> NVENC lossless (_hw_hevc_lossless).
        # A mono/Bayer plane pins full range (formats.LUMA_COLORIMETRY) so videoconvert copies it into
        # Y verbatim; colour keeps its own colorimetry. Fed NV12, the frame arrives ready for
        # nvvidconv and there is nothing to convert on the CPU.
        head = "queue max-size-buffers=12 name=rec_q ! "
        if not nv12:
            nv24 = "video/x-raw,format=NV24" + (f",colorimetry={luma}" if luma else "")
            head += f"{vconv} ! {nv24} ! "
        return head + _hw_hevc_lossless(cfg, gop, bframes) + " ! " + sink, enc

    if enc == "x265-lossless":
        # CPU lossless + temporal; 8-bit only (>8-bit fell back to ffv1 above). Throughput-limited
        # at 4K. Mono/Bayer: pin I420 at full range so the (mosaic) plane rides bit-exact in Y with
        # neutral chroma -- unpinned, videoconvert is free to negotiate a format or range that munges
        # the data. Color keeps the unpinned negotiation (use ffv1 for guaranteed-bit-exact color).
        opts = "lossless=1"
        if gop:
            opts += f":keyint={gop}:min-keyint={gop}"
        opts += f":bframes={bframes}"
        pin = f"video/x-raw,format=I420,colorimetry={luma} ! " if luma else ""
        return (
            f"queue max-size-buffers=12 name=rec_q ! {vconv} ! {pin}"
            f'x265enc option-string="{opts}" speed-preset=ultrafast ! h265parse ! ' + sink
        ), enc

    # ffv1 (default for >8-bit): truly lossless, high-bit-depth, but INTRA-only -> the temporal knobs
    # (keyframe_interval_s / bframes) don't apply.
    if gop or bframes:
        log.info("recorder: ffv1 is intra-only; ignoring keyframe_interval_s/bframes")
    # threads/slices: avenc_ffv1 defaults to ONE thread, which caps 16-bit 640x512 at ~26-29 fps on an
    # Orin ARM core -- measured in the field as a permanent recorder stall against a 60 fps thermal
    # camera (the tee blocks, consumers starve, the feeder drops half the frames). FFV1 parallelizes
    # across slices (version 3 of the bitstream), so slices=4 + threads=0 (auto) restores real-time
    # with headroom. slicecrc protects each slice; lossless fidelity is unaffected by slicing.
    return (
        f"queue max-size-buffers=12 name=rec_q ! {vconv} ! "
        "avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1 ! " + sink
    ), enc


# ---- proving the NV12 feed ---------------------------------------------------------------------
_PROBE_ELEMENTS = ("nvvidconv", "nvv4l2h265enc", "h265parse", "avdec_h265", "capssetter", "videoconvert")
_probe_verdicts: dict = {}       # (width, height) -> True / False; an inconclusive run is not kept


def _probe_frames(width: int, height: int) -> list:
    """Three frames that between them put every 8-bit code at many positions: a ramp, noise (the worst
    case for the codec, and the second frame is predicted from the first), and the ramp reversed."""
    row = (bytes(range(256)) * (width // 256 + 1))[:width]
    half = height // 2
    ramp = row * half + (row[97:] + row[:97]) * (height - half)
    return [ramp, os.urandom(width * height), ramp[::-1]]


def probe_nv12_feed(width: int, height: int, cfg=None, timeout_s: float = 10.0) -> Optional[bool]:
    """Is the NV12 feed bit-exact on THIS host at THIS geometry? True, False, or None when the
    question could not be put (an element missing, the encoder would not start, a timeout).

    Frames go through the recorder's own hardware fragment and come back through the service's own
    read path (sources.replay.raw_decode_chain); every byte of the plane must match. It has to run on
    the host and at the geometry in use, because what it guards against has depended on both before:
    the VIC decides by input format whether to treat a plane as full or limited range (NV12 passes
    verbatim on R39, I420 and GRAY8 do not), and the height-dependent default of the software
    conversion went unnoticed for months behind a 512-row test. A verdict is remembered for the life
    of the process; a JetPack change is a new process."""
    key = (int(width), int(height))
    if key in _probe_verdicts:
        return _probe_verdicts[key]
    missing = [e for e in _PROBE_ELEMENTS if Gst.ElementFactory.find(e) is None]
    if missing or not nv12_geometry_ok(width, height):
        log.info("recorder: NV12 feed not probed at %dx%d (%s)", width, height,
                 "missing " + ", ".join(missing) if missing else "geometry needs width % 4 == 0 and an even height")
        return None
    from .sources.replay import raw_decode_chain      # the read path; imported late (it imports the sources)
    frames = _probe_frames(width, height)
    chroma = ChromaBlock(width, height)
    decode = raw_decode_chain("video/x-h265", {"recording_colorimetry": LUMA_COLORIMETRY})
    desc = (f'appsrc name=s format=time caps="{nv12_feed_caps(width, height, "30/1")}" ! queue ! '
            f"{_hw_hevc_lossless(cfg, 0, 0)} ! {decode} ! videoconvert ! video/x-raw,format=GRAY8 ! "
            "appsink name=o sync=false max-buffers=8")
    pipeline, got, failure = None, [], None
    try:
        pipeline = Gst.parse_launch(desc)
        src, sink = pipeline.get_by_name("s"), pipeline.get_by_name("o")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("the probe pipeline refused to start")
        for i, frame in enumerate(frames):
            buf = chroma.tail(Gst.Buffer.new_wrapped(frame))
            buf.pts, buf.duration = i * Gst.SECOND // 30, Gst.SECOND // 30
            if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                raise RuntimeError("the probe pipeline refused a frame")
        src.emit("end-of-stream")
        while len(got) < len(frames):
            sample = sink.emit("try-pull-sample", int(timeout_s * Gst.SECOND))
            if sample is None:
                break
            got.append(sample.get_buffer().extract_dup(0, width * height))
        err = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if err is not None:
            failure = err.parse_error()[0].message
    except Exception as e:   # noqa: BLE001 -- a probe that cannot run must leave the old path in place
        failure = str(e)
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
    bad = [i for i, (a, b) in enumerate(zip(got, frames)) if a != b]
    if bad:
        a, b = got[bad[0]], frames[bad[0]]
        worst = max(abs(x - y) for x, y in zip(a[::61], b[::61]))
        log.warning("recorder: the NV12 feed is NOT bit-exact on this host at %dx%d (frame %d of the probe "
                    "differs, sampled error up to %d codes); keeping the videoconvert feed", width, height,
                    bad[0], worst)
        _probe_verdicts[key] = False
        return False
    if failure is not None or len(got) < len(frames):
        log.warning("recorder: NV12 feed probe inconclusive at %dx%d (%s); keeping the videoconvert feed",
                    width, height, failure or f"{len(got)} of {len(frames)} frames came back")
        return None
    log.info("recorder: NV12 feed verified bit-exact at %dx%d (%d frames, every 8-bit code)",
             width, height, len(frames))
    _probe_verdicts[key] = True
    return True
