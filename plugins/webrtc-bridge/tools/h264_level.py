#!/usr/bin/env python3
"""Pure, table-driven H.264 level math for the WebRTC encoder (no GStreamer dependency).

WHY: a level is the decoder-workload ceiling advertised in the SDP -- the low byte of the H.264
`profile-level-id`. If webrtcsink advertises a level BELOW what it actually encodes -- the bug this
fixes: a FIXED profile-level-id=42e01f (Level 3.1, ~1280x720@30) sent for a 2048x1536 stream -- the
browser receives RTP but can't decode the out-of-level frames (framesDecoded ~0, a black tile). So we
derive the LOWEST level that COVERS the resolution+fps actually fed to the encoder and put it in the
encoder OUTPUT caps; the RTP payloader then emits a matching profile-level-id (verified end to end:
caps level=5 -> profile-level-id ...c032).

The limits are ITU-T H.264 Annex A, Table A-1, and are PROFILE-INDEPENDENT (MaxFS/MaxMBPS don't vary by
profile), so the same math serves both constrained-baseline and high.

Level strings are GStreamer-CANONICAL: whole levels are "3"/"4"/"5" (NOT "3.0"/"4.0"/"5.0" -- those are
rejected by H.264 caps negotiation). e.g. h264_level_for(1920, 1080, 30) -> "4".
"""
import math

# (gst level string, MaxFS [macroblocks/frame], MaxMBPS [macroblocks/sec], MaxBR [kbps]). Ordered
# low -> high. MaxBR is the Baseline/Main ceiling (High allows 1.25x) -- a CONSERVATIVE optional gate;
# FS/MBPS (decode workload) are the dominant constraints and the only ones used unless a bitrate is
# passed. Rows 3.0+ match the brief's table; 1..2.2 are the standard lower levels for completeness.
_LEVELS = [
    ("1",     99,    1485,    64),
    ("1b",    99,    1485,    128),
    ("1.1",   396,   3000,    192),
    ("1.2",   396,   6000,    384),
    ("1.3",   396,   11880,   768),
    ("2",     396,   11880,   2000),
    ("2.1",   792,   19800,   4000),
    ("2.2",   1620,  20250,   4000),
    ("3",     1620,  40500,   10000),
    ("3.1",   3600,  108000,  14000),
    ("3.2",   5120,  216000,  20000),
    ("4",     8192,  245760,  20000),
    ("4.1",   8192,  245760,  50000),
    ("4.2",   8704,  522240,  50000),
    ("5",     22080, 589824,  135000),
    ("5.1",   36864, 983040,  240000),
    ("5.2",   36864, 2073600, 240000),
]

LEVELS = [row[0] for row in _LEVELS]          # valid gst level strings (for config validation)
_INDEX = {row[0]: i for i, row in enumerate(_LEVELS)}


def _macroblocks(width, height):
    # H.264 codes in 16x16 macroblocks; a partial block still costs a whole one -> round up.
    return math.ceil(width / 16) * math.ceil(height / 16)


def h264_level_for(width, height, fps, bitrate_kbps=None, max_level="5.2"):
    """Lowest H.264 level (gst caps string) covering width x height @ fps -- and, when bitrate_kbps is
    given, also that bitrate -- clamped to max_level. Profile-independent. Raises ValueError on bad
    input. Beyond the table's ceiling (or the clamp) it returns max_level: pair with level_covers() to
    warn that the resolution won't decode in-spec (downscale or raise the clamp)."""
    if None in (width, height, fps) or min(width, height, fps) <= 0:
        raise ValueError("width/height/fps must be positive (got %r)" % ((width, height, fps),))
    if max_level not in _INDEX:
        raise ValueError("unknown max_level %r (want one of %s)" % (max_level, LEVELS))
    fs = _macroblocks(width, height)
    mbps = fs * int(round(fps))
    cap = _INDEX[max_level]
    for i, (lvl, maxfs, maxmbps, maxbr) in enumerate(_LEVELS):
        if maxfs >= fs and maxmbps >= mbps and (bitrate_kbps is None or maxbr >= bitrate_kbps):
            return _LEVELS[min(i, cap)][0]            # first (lowest) fit, never above the clamp
    return _LEVELS[cap][0]                            # beyond 5.2 entirely -> the clamp (caller warns)


def level_covers(level, width, height, fps, bitrate_kbps=None):
    """Does `level` actually cover width x height @ fps[ + bitrate]? Lets the caller detect when a
    max_level clamp (or the 5.2 ceiling) is BELOW what the resolution needs and warn -- the advertised
    stream would then be out-of-level and not decode."""
    if level not in _INDEX:
        return False
    _, maxfs, maxmbps, maxbr = _LEVELS[_INDEX[level]]
    fs = _macroblocks(width, height)
    return (maxfs >= fs and maxmbps >= fs * int(round(fps))
            and (bitrate_kbps is None or maxbr >= bitrate_kbps))
