"""Sync-point (keyframe) detection on delivered ENCODED bytes -- pure Python, no GStreamer.

A recording session that opens mid-stream on an H.264/H.265 source must begin on a keyframe: the
stream-copy recorder muxes the delivered bitstream verbatim, and a parser does NOT drop pre-IDR data,
so a session started on a P-frame would produce a first segment whose head no decoder can render.
Every LATER segment starts on a keyframe because splitmuxsink splits there; this gate is what makes
segment 00000 like the others. MJPEG is all-intra, so every frame qualifies.

The check reads the bytes the source already hands on_encoded (plus the caps string it delivers with
them) rather than a GstBuffer flag: a DELTA_UNIT flag is reliable behind rtph264depay/x264enc and
unreliable behind uvcvideo, and it would be one more contract to keep in step across sources.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# H.264 NAL types (nal_unit_type, low 5 bits of the header byte)
_H264_IDR = 5
# H.265 NAL types (6 bits, header byte >> 1): BLA_W_LP..CRA_NUT are the random-access points
_H265_IRAP = range(16, 22)

_MEDIA_RE = re.compile(r"^\s*([A-Za-z0-9-]+/[A-Za-z0-9.+-]+)")
_STREAM_FORMAT_RE = re.compile(r"stream-format=(?:\(string\))?([A-Za-z0-9-]+)")
_CODEC_DATA_RE = re.compile(r"codec_data=(?:\(buffer\))?([0-9A-Fa-f]+)")

_unknown_warned = set()


def parse_caps_kind(caps_str: Optional[str]) -> Tuple[Optional[str], Optional[str], int]:
    """(media_type, stream_format, nal_length_size) from a GStreamer caps STRING such as
    'video/x-h264, stream-format=(string)avc, codec_data=(buffer)0164...'. The NAL length size is
    read from the avcC / hvcC codec_data when present (lengthSizeMinusOne + 1), else 4."""
    if not caps_str:
        return None, None, 4
    m = _MEDIA_RE.match(caps_str)
    media = m.group(1) if m else None
    sf = _STREAM_FORMAT_RE.search(caps_str)
    stream_format = sf.group(1) if sf else None
    nal_len = 4
    cd = _CODEC_DATA_RE.search(caps_str)
    if cd:
        blob = cd.group(1)
        try:
            if media == "video/x-h264" and len(blob) >= 10:
                nal_len = (int(blob[8:10], 16) & 0x3) + 1          # avcC byte 4
            elif media == "video/x-h265" and len(blob) >= 44:
                nal_len = (int(blob[42:44], 16) & 0x3) + 1         # hvcC byte 21
        except ValueError:
            pass
    return media, stream_format, nal_len


def _annexb_nal_headers(data: bytes):
    """Yield the NAL header byte(s) offset after each Annex B start code (00 00 01 / 00 00 00 01)."""
    i = 0
    n = len(data)
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0:
            return
        i += 3
        if i < n:
            yield i


def _length_prefixed_nal_headers(data: bytes, nal_len: int):
    """Yield the header offset of each length-prefixed NAL; a size that overruns the data is
    truncation/corruption, and a truncated NAL is never trusted as a sync point."""
    i = 0
    n = len(data)
    while i + nal_len < n:
        size = int.from_bytes(data[i:i + nal_len], "big")
        if size <= 0 or i + nal_len + size > n:
            return
        yield i + nal_len
        i += nal_len + size


def is_sync_point(media_type: Optional[str], data: bytes, stream_format: Optional[str] = None,
                  nal_length_size: int = 4) -> bool:
    """True iff `data` (one delivered encoded frame / access unit) can start a recording: an IDR for
    H.264, an IRAP (BLA/IDR/CRA) for H.265, any frame for MJPEG. Empty/truncated H.26x data is False.
    An unrecognised media type is passed through (True, logged once) -- refusing would silently
    record nothing for a codec this check simply doesn't know."""
    if media_type == "image/jpeg":
        return True
    if media_type in ("video/x-h264", "video/x-h265"):
        if not data:
            return False
        prefixed = stream_format in ("avc", "avc3", "hvc1", "hev1")
        headers = (_length_prefixed_nal_headers(data, nal_length_size) if prefixed
                   else _annexb_nal_headers(data))
        for off in headers:
            hdr = data[off]
            if media_type == "video/x-h264":
                if (hdr & 0x1F) == _H264_IDR:
                    return True
            elif ((hdr >> 1) & 0x3F) in _H265_IRAP:
                return True
        return False
    if media_type not in _unknown_warned:
        _unknown_warned.add(media_type)
        log.info("keyframe gate: no sync-point rule for %r; recording from the first delivered frame",
                 media_type)
    return True
