"""Pixel-format parsing + encoder selection (pure logic; no GStreamer -- unit-testable).

Two source-string conventions feed in:
  * GVSP/Aravis (gige):  Mono8, Mono16, BayerRG8, ... -> mapped to a GStreamer raw format.
  * GStreamer-native (usb/v4l2, decoded rtsp):  GRAY8, I420, NV12, YUY2, RGB, ... -> as-is.

Color formats (YUV/RGB) record via FFV1 to stay BIT-EXACT: the hw-hevc path converts to
NV24 (4:4:4), which for a 4:2:0 source (I420/NV12) is an upsample->encode->downsample round
trip = not lossless. FFV1 keeps the source's native subsampling. (NVENC color-lossless fed
the native subsampling -- e.g. NV12 -- is a hardware refinement to verify on-device.)

Already-encoded sources (MJPEG USB, H.264/H.265 RTSP) record via STREAM-COPY: the delivered
bitstream is muxed verbatim -- faithful to what the host received, and no pointless re-encode
of already-lossy video. See encoded_info + select_encoder.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_BAYER_MAP = {"RG": "rggb", "GR": "grbg", "GB": "gbrg", "BG": "bggr"}

# GStreamer raw formats accepted verbatim from a source (usb / decoded rtsp).
_GST_MONO = {"GRAY8", "GRAY16_LE", "GRAY16_BE"}
_GST_COLOR = {"I420", "NV12", "NV24", "YV12", "YUY2", "UYVY",
              "RGB", "BGR", "RGBA", "BGRA", "RGBx", "BGRx"}
_GST_RAW = _GST_MONO | _GST_COLOR

VALID_ENCODERS = ("auto", "hw-hevc-lossless", "ffv1", "x265-lossless", "stream-copy")

VALID_DECODERS = ("auto", "software")

# Encoded delivery (already-compressed sources: MJPEG USB, H.264/H.265 RTSP). Per codec:
#   src_caps -- caps the source tags the encoded buffers with (= the recorder appsrc caps)
#   parser   -- the recorder stream-copies through this into the muxer (no re-encode)
#   decoder  -- SOFTWARE decoder for the raw consumer path (HW nvv4l2decoder = hardware refinement)
_ENCODED = {
    "MJPEG": ("image/jpeg", "jpegparse", "jpegdec"),
    "JPEG":  ("image/jpeg", "jpegparse", "jpegdec"),
    "H264":  ("video/x-h264", "h264parse", "avdec_h264"),
    "H265":  ("video/x-h265", "h265parse", "avdec_h265"),
}


def encoded_info(fmt):
    """(src_caps, parser, decoder) for an encoded codec string, or None if `fmt` is raw."""
    return _ENCODED.get((fmt or "").upper())


# Per-codec Jetson HW decoder, used only where it's a clear win. H.264/H.265 -> nvv4l2decoder (NVMM
# out, nvvidconv-converted): software H.26x decode is heavy, so HW pays off.
# MJPEG is deliberately NOT here: measured on a JP7 Orin, HW nvjpegdec (nvjpegdec ! NVMM ! nvvidconv)
# is SLOWER than CPU jpegdec at 720p -- the NVMM round-trip costs more than the decode saves -- and the
# real consumer ceiling is the per-frame Python copy/transport, not the decode. So MJPEG stays on
# software jpegdec (and the stream-copy recording is faithful regardless of the consumer decode path).
_HW_DECODER = {
    "avdec_h264": ("nvv4l2decoder", "nvvidconv"),
    "avdec_h265": ("nvv4l2decoder", "nvvidconv"),
}


def select_decoder(sw_decoder, hw_available=False, mode="auto"):
    """(decoder, converter) for the encoded->raw consumer branch. On a Jetson with the L4T plugins
    present, the per-codec HW decoder (nvv4l2decoder for H.26x, nvjpegdec for MJPEG) + nvvidconv
    (NVMM->system); otherwise the software decoder + CPU videoconvert. `decoder` may be a multi-element
    fragment (the nvjpegdec NVMM hint) -- callers splice it as `... ! {parser} ! {decoder} ! {conv} ! ...`.

    HW selection is AUTOMATIC (caller passes hw_available = the L4T HW decoder present), so a JP6->JP7
    host upgrade activates NVDEC with no code change -- only a deploy that exposes the GPU (CDI
    --device nvidia.com/gpu=all on JP7). Recording is stream-copy and never decodes, so this affects
    only the live-consumer path.

    `mode="software"` (per-sensor `decoder:` config) forces the software decoder even with HW present:
    for streams HW decode cannot START on. The motivating case is the SIYI ZR30's live RTSP H.265,
    which carries NO random-access point, ever (no IDR/CRA NALs -- rolling intra refresh only;
    verified by NAL histogram, 45 s / 1076 frames / 0 IRAP). nvv4l2decoder correctly waits forever
    for a sync point, while avdec converges to clean output after one refresh sweep and holds
    4K@25 on an Orin CPU core."""
    if mode not in VALID_DECODERS:
        log.warning("unknown decoder mode %r; falling back to auto", mode)
        mode = "auto"
    if mode != "software" and hw_available and sw_decoder in _HW_DECODER:
        return _HW_DECODER[sw_decoder]
    return sw_decoder, "videoconvert"


def parse_pixel_format(pixel_format):
    """Return (gst_format, bits_per_pixel, bayer_pattern, packed, is_color)."""
    pf = pixel_format or "Mono8"
    if pf in _GST_RAW:                        # already a GStreamer raw format (usb / decoded)
        bits = 16 if "16" in pf else 8
        return pf, bits, None, False, pf in _GST_COLOR
    # GVSP/Aravis style: Mono* / Bayer*
    bits = 16 if any(tok in pf for tok in ("16", "12", "10")) else 8
    packed = pf.endswith("p") or "Packed" in pf
    bayer = _BAYER_MAP.get(pf[5:7].upper()) if pf.startswith("Bayer") and len(pf) >= 7 else None
    gst_format = "GRAY16_LE" if bits > 8 else "GRAY8"
    return gst_format, bits, bayer, packed, False


def bytes_per_frame(gst_format, width, height):
    """Frame size in bytes for a GStreamer raw format (accounts for chroma subsampling)."""
    px = int(width) * int(height)
    if gst_format in ("GRAY16_LE", "GRAY16_BE"):
        return px * 2
    if gst_format in ("I420", "NV12", "YV12"):     # 4:2:0
        return px * 3 // 2
    if gst_format in ("YUY2", "UYVY"):             # 4:2:2 packed
        return px * 2
    if gst_format == "NV24":                       # 4:4:4
        return px * 3
    if gst_format in ("RGB", "BGR"):
        return px * 3
    if gst_format in ("RGBA", "BGRA", "RGBx", "BGRx"):
        return px * 4
    return px                                      # GRAY8 / 8-bit single plane (incl. Bayer8)


def select_encoder(encoder, bits_per_pixel, is_color=False, encoded=False):
    """Resolve `auto`: an already-encoded source -> stream-copy (mux the delivered bitstream
    verbatim -- faithful + no pointless re-encode); color -> ffv1 (lossless, no chroma
    resample); mono/Bayer 8-bit -> hw-hevc-lossless; >8-bit -> ffv1. An explicit (non-auto)
    encoder is honored (e.g. force ffv1 to re-encode a decoded encoded source)."""
    if encoder not in VALID_ENCODERS:
        log.warning("unknown encoder %r; falling back to auto", encoder)
        encoder = "auto"
    if encoder != "auto":
        return encoder
    if encoded:
        return "stream-copy"
    if is_color:
        return "ffv1"
    return "hw-hevc-lossless" if bits_per_pixel <= 8 else "ffv1"
