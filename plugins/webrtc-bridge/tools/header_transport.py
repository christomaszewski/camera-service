"""Headered-shm (JP6 plugin endpoint) frame parsing for the webrtc bridge (pure logic; no
GStreamer -- unit-testable anywhere, see test_header_transport.py).

The core's JP6 plugin endpoint carries [ 36-byte FrameHeader ][ raw pixel bytes ] per buffer
under `application/x-cam-frame` caps (shm drops caps/PTS/meta, so per-frame metadata rides
in-band). This module is the bridge-side mirror of that CONTRACT -- the source of truth is
core-driver/cam_driver/transport.py; the C++ consumers mirror it too
(plugins/ros2-bridge/src/cam_header_bridge.cpp, plugins/ros1-bridge/src/cam_ros1_bridge.cpp).
The layout is vendored here (not imported) because the bridge image doesn't ship the core
package; `version` + `header_len` provide the forward-compat seam, exactly like the C++ side.

What the bridge gains over the raw endpoint by consuming this:
  - self-describing geometry/format (no CAM_WIDTH/HEIGHT/FORMAT env prediction to drift), and
  - the ABSOLUTE capture timestamp + provenance, which the pump re-attaches to each buffer
    (offset=frame_id, offset_end=capture-ns -- the same convention the core uses on unixfd),
    making capture->encode latency measurable in-bridge on JP6.
"""
import struct
from dataclasses import dataclass

MAGIC = b"CAMF"
VERSION = 1
_FORMAT = "<4sHHQQHHIBBH"
HEADER_SIZE = struct.calcsize(_FORMAT)  # 36

# pixfmt code -> GStreamer raw format (transport.py _CODE_TO_GST; additive, never reordered).
# Bayer mosaics ride as their byte-identical GRAY8 plane -- CFA-ness is config-side (CAM_BAYER).
CODE_TO_GST = {1: "GRAY8", 2: "GRAY16_LE", 3: "GRAY16_BE",
               4: "I420", 5: "NV12", 6: "YUY2", 7: "RGB", 8: "BGR",
               9: "NV24", 10: "YV12", 11: "UYVY",
               12: "RGBA", 13: "BGRA", 14: "RGBx", 15: "BGRx"}

# ts_source code -> provenance name (transport.py TS_SOURCE_CODE / cam_driver.timestamps ladder),
# + a short form for the burned-in overlay. Unknown codes are additive-safe: keep the number.
TS_SOURCE_NAME = {0: "ptp_chunk", 1: "camera", 2: "system", 3: "sof", 4: "rtp_ntp"}
TS_SOURCE_SHORT = {"ptp_chunk": "ptp", "camera": "cam", "system": "sys",
                   "sof": "sof", "rtp_ntp": "rtp"}


class HeaderError(ValueError):
    pass


@dataclass
class FrameInfo:
    timestamp_ns: int          # absolute capture time (PTP epoch when locked)
    frame_id: int
    width: int
    height: int
    pixfmt: str                # GStreamer raw format string, e.g. "GRAY8"
    ts_source: str             # provenance name (see TS_SOURCE_NAME)
    header_len: int            # offset to pixel data (>= HEADER_SIZE on v2+ headers)


def parse_header(data):
    """Parse the leading FrameHeader from a transport buffer (bytes-like; may include pixels).

    Mirrors the C++ bridge's validation: magic, version, and -- because header_len is wire data
    that becomes a pixel offset -- its bounds against the actual buffer, so a corrupt header can
    never slice past the mapped region."""
    if len(data) < HEADER_SIZE:
        raise HeaderError("buffer too small for header: {} < {}".format(len(data), HEADER_SIZE))
    magic, version, header_len, ts, fid, w, h, pixfmt, src, _flags, _ = struct.unpack(
        _FORMAT, bytes(data[:HEADER_SIZE]))
    if magic != MAGIC:
        raise HeaderError("bad magic {!r}".format(magic))
    if version != VERSION:
        raise HeaderError("unsupported header version {} (this build: {})".format(version, VERSION))
    if header_len < HEADER_SIZE or header_len > len(data):
        raise HeaderError("bad header_len {} (buffer {})".format(header_len, len(data)))
    fmt = CODE_TO_GST.get(pixfmt)
    if fmt is None:
        raise HeaderError("unknown pixfmt code {}".format(pixfmt))
    if not (w and h):
        raise HeaderError("bad geometry {}x{}".format(w, h))
    return FrameInfo(timestamp_ns=ts, frame_id=fid, width=w, height=h, pixfmt=fmt,
                     ts_source=TS_SOURCE_NAME.get(src, str(src)), header_len=header_len)


def caps_for_frame(info, bayer=None, debayer=True, fps=None):
    """The caps string the pump stamps on its appsrc for this frame's header.

    The wire carries a Bayer mosaic as GRAY8 (CODE_TO_GST has no CFA entries), so CFA-ness comes
    from config: a non-empty `bayer` pattern relabels an 8-bit plane `video/x-bayer` when
    debayering is wanted -- byte-identical, and a WRONG pattern only mis-tints the preview (it
    cannot crash negotiation, unlike the old raw-shm static front-end). debayer=False (or a
    non-GRAY8 format, e.g. a 16-bit sensor) keeps the raw header format. `fps` is a config HINT
    (the header carries no rate); it feeds the H.264 level derivation downstream."""
    if info.pixfmt == "GRAY8" and bayer and debayer:
        s = "video/x-bayer,format={}".format(bayer)
    else:
        s = "video/x-raw,format={}".format(info.pixfmt)
    s += ",width={},height={}".format(info.width, info.height)
    if fps:
        s += ",framerate={}/1".format(int(fps))
    return s


class PtsTracker:
    """Absolute capture-ns -> RELATIVE, strictly-monotonic PTS. Mirrors the core's policy
    (cam_driver/pipeline.py _on_frame): an absolute-ns PTS stalls downstream flow, and a camera
    clock reset across a reconnect must not push time backward through the muxer/payloader --
    on a non-monotonic stamp, rebase so PTS keeps advancing by the last observed interval."""

    def __init__(self, fallback_interval_ns=40_000_000):
        self._base = None
        self._last = None
        self._iv = fallback_interval_ns

    def pts_for(self, ts_ns):
        if self._base is None:
            self._base = ts_ns
        pts = ts_ns - self._base
        if self._last is not None:
            if pts <= self._last:
                self._base = ts_ns - (self._last + self._iv)
                pts = self._last + self._iv
            else:
                self._iv = pts - self._last
        self._last = pts
        return pts
