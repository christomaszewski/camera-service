"""Pluggable lossless recorder branch, built as a GStreamer launch fragment.

Encoder selection (``auto``):
  * 8-bit input  -> hardware HEVC lossless (NVENC, NV24/YUV444; bit-exact, temporal).
                    A mono/Bayer mosaic rides in the Y plane (chroma neutral) and is
                    recovered by dropping chroma in post.
  * >8-bit input -> FFV1 (lossless, high-bit-depth; INTRA-only, no temporal) by
                    default, or x265 --lossless (temporal, CPU-bound) if requested.

Validated on a JetPack 7.2 Orin AGX (L4T r39.x, driver R595.78): the
NV24/NVMM -> nvv4l2h265enc enable-lossless=1 path is BIT-EXACT for 8-bit mono --
60/60 random-noise frames round-trip with worst |delta| = 0, and the encoded stream is
1.32x raw (incompressible noise can't shrink, which is the proof it's truly lossless).
The GRAY8 -> NV24 conversion keeps full range, so there's no [16,235] clamp. Re-verify
after a JetPack bump (NVMM caps negotiation + the lossless enum are L4T-version-dependent):
`tools/nvenc_lossless_test.sh` (or .py) with the NVENC CDI device.
"""
from __future__ import annotations

import logging

from gi.repository import Gst

log = logging.getLogger(__name__)

VALID_ENCODERS = ("auto", "hw-hevc-lossless", "ffv1", "x265-lossless")

# nvv4l2h265enc preset-level enum (HW search depth: bigger = smaller lossless file, slower encode).
_PRESET_LEVEL = {"disable": 0, "ultrafast": 1, "fast": 2, "medium": 3, "slow": 4}


def _preset_level(value):
    """Map an nvenc preset name/number to its preset-level int, or None to leave the encoder default."""
    s = str(value or "").strip().lower()
    if s in _PRESET_LEVEL:
        return _PRESET_LEVEL[s]
    if s.isdigit() and 0 <= int(s) <= 4:
        return int(s)
    return None


def select_encoder(encoder: str, bits_per_pixel: int) -> str:
    if encoder not in VALID_ENCODERS:
        log.warning("unknown encoder %r; falling back to auto", encoder)
        encoder = "auto"
    if encoder != "auto":
        return encoder
    return "hw-hevc-lossless" if bits_per_pixel <= 8 else "ffv1"


def _splitmux(location_base: str, seconds: int, muxer: str = "matroskamux") -> str:
    # splitmuxsink preserves continuous PTS across segments (good for alignment). send-keyframe-requests
    # makes it ask the encoder for a keyframe at each split, so every .mkv starts on a keyframe and is
    # independently decodable even with a long GOP.
    max_ns = max(1, int(seconds)) * Gst.SECOND
    return (f'splitmuxsink name=rec_sink muxer={muxer} send-keyframe-requests=true '
            f'location="{location_base}-%05d.mkv" max-size-time={max_ns}')


def _gop_frames(cfg, fps: float) -> int:
    """Keyframe interval in frames from the configured seconds-window (0 -> 0 = encoder default)."""
    if cfg.keyframe_interval_s and cfg.keyframe_interval_s > 0:
        return max(1, int(round(cfg.keyframe_interval_s * (fps if fps and fps > 0 else 30.0))))
    return 0


def build_recorder_description(cfg, bits_per_pixel: int, location_base: str, fps: float = 0.0) -> str:
    """Return a gst-launch fragment beginning with a sink pad (linkable from `tee.` or an appsrc)."""
    enc = select_encoder(cfg.encoder, bits_per_pixel)
    sink = _splitmux(location_base, cfg.segment_seconds)
    gop = _gop_frames(cfg, fps)
    bframes = max(0, int(getattr(cfg, "bframes", 0)))
    preset = _preset_level(getattr(cfg, "nvenc_preset", "")) if enc == "hw-hevc-lossless" else None
    log.info("recorder: encoder=%s (bits=%d) gop=%s bframes=%d preset=%s -> %s-*.mkv",
             enc, bits_per_pixel, gop or "default", bframes,
             "default" if preset is None else preset, location_base)

    if enc == "hw-hevc-lossless":
        # GRAY8 / Bayer8 mosaic -> Y plane of NV24 -> NVMM -> NVENC lossless. iframeinterval = the
        # GOP/keyframe window; preset-level = HW search depth (bigger = smaller file, slower -- the lever
        # for archival size, but must sustain the frame rate); num-B-Frames only when asked (Xavier-only
        # on Tegra). Property names/lossless interplay are L4T-version-dependent.
        opts = f" maxperf-enable={1 if getattr(cfg, 'nvenc_maxperf', True) else 0}"
        level = _preset_level(getattr(cfg, "nvenc_preset", ""))
        if level is not None:
            opts += f" preset-level={level}"
        if gop:
            opts += f" iframeinterval={gop}"
        if bframes:
            opts += f" num-B-Frames={bframes}"
        return (
            "queue max-size-buffers=12 name=rec_q ! "
            "videoconvert ! video/x-raw,format=NV24 ! "
            "nvvidconv ! video/x-raw(memory:NVMM),format=NV24 ! "
            f"nvv4l2h265enc enable-lossless=1{opts} ! h265parse ! " + sink
        )

    if enc == "x265-lossless":
        # CPU lossless + temporal; keeps high bit depth. Throughput-limited at 4K.
        opts = "lossless=1"
        if gop:
            opts += f":keyint={gop}:min-keyint={gop}"
        opts += f":bframes={bframes}"
        return (
            "queue max-size-buffers=12 name=rec_q ! videoconvert ! "
            f'x265enc option-string="{opts}" speed-preset=ultrafast ! h265parse ! ' + sink
        )

    # ffv1 (default for >8-bit): truly lossless, high-bit-depth, but INTRA-only -> the temporal knobs
    # (keyframe_interval_s / bframes) don't apply.
    if gop or bframes:
        log.info("recorder: ffv1 is intra-only; ignoring keyframe_interval_s/bframes")
    return (
        "queue max-size-buffers=12 name=rec_q ! videoconvert ! "
        "avenc_ffv1 coder=1 context=1 ! " + sink
    )
