"""Pluggable lossless recorder branch, built as a GStreamer launch fragment.

Encoder selection (``auto``):
  * 8-bit input  -> hardware HEVC lossless (NVENC, NV24/YUV444; bit-exact, temporal).
                    A mono/Bayer mosaic rides in the Y plane (chroma neutral) and is
                    recovered by dropping chroma in post.
  * >8-bit input -> FFV1 (lossless, high-bit-depth; INTRA-only, no temporal) by
                    default, or x265 --lossless (temporal, CPU-bound) if requested.

!! ON-DEVICE VALIDATION REQUIRED !!
The Jetson hardware path below is written to the research but NOT yet validated on
hardware. NVMM caps negotiation is finicky and the lossless profile enum is
L4T-version-dependent. Before trusting it: run `gst-inspect-1.0 nvv4l2h265enc`
on the target, confirm `enable-lossless` + NV24, and verify a bit-exact round trip
(ffmpeg framemd5 / PSNR=inf). See README "Post-processing".
"""
from __future__ import annotations

import logging

from gi.repository import Gst

log = logging.getLogger(__name__)

VALID_ENCODERS = ("auto", "hw-hevc-lossless", "ffv1", "x265-lossless")


def select_encoder(encoder: str, bits_per_pixel: int) -> str:
    if encoder not in VALID_ENCODERS:
        log.warning("unknown encoder %r; falling back to auto", encoder)
        encoder = "auto"
    if encoder != "auto":
        return encoder
    return "hw-hevc-lossless" if bits_per_pixel <= 8 else "ffv1"


def _splitmux(location_base: str, seconds: int, muxer: str = "matroskamux") -> str:
    # splitmuxsink preserves continuous PTS across segments (good for alignment).
    max_ns = max(1, int(seconds)) * Gst.SECOND
    return (f'splitmuxsink name=rec_sink muxer={muxer} '
            f'location="{location_base}-%05d.mkv" max-size-time={max_ns}')


def build_recorder_description(cfg, bits_per_pixel: int, location_base: str) -> str:
    """Return a gst-launch fragment beginning with a sink pad (linkable from `tee.`)."""
    enc = select_encoder(cfg.encoder, bits_per_pixel)
    sink = _splitmux(location_base, cfg.segment_seconds)
    log.info("recorder: encoder=%s (bits=%d) -> %s-*.mkv", enc, bits_per_pixel, location_base)

    if enc == "hw-hevc-lossless":
        # GRAY8 / Bayer8 mosaic -> Y plane of NV24 -> NVMM -> NVENC lossless (temporal).
        return (
            "queue max-size-buffers=12 name=rec_q ! "
            "videoconvert ! video/x-raw,format=NV24 ! "
            "nvvidconv ! video/x-raw(memory:NVMM),format=NV24 ! "
            "nvv4l2h265enc enable-lossless=1 maxperf-enable=1 ! h265parse ! " + sink
        )

    if enc == "x265-lossless":
        # CPU lossless + temporal; keeps high bit depth. Throughput-limited at 4K.
        return (
            "queue max-size-buffers=12 name=rec_q ! videoconvert ! "
            'x265enc option-string="lossless=1" speed-preset=ultrafast ! h265parse ! ' + sink
        )

    # ffv1 (default for >8-bit): truly lossless, high-bit-depth, but INTRA-only.
    return (
        "queue max-size-buffers=12 name=rec_q ! videoconvert ! "
        "avenc_ffv1 coder=1 context=1 ! " + sink
    )
