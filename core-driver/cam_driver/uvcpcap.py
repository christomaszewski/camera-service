"""UVC frame extraction from Linux usbmon captures (pure logic; no GStreamer).

Feeds the pcap replay source: read a Wireshark-on-Linux capture of a USB (UVC) camera,
find the video streaming endpoint, reassemble UVC payload transfers into complete
frames, and yield (capture_ts_ns, frame_bytes) in order -- streaming, O(1) memory.

Layering: pcapio yields (link_type, ts_ns, record_bytes); this module accepts the two
Linux usbmon link types and knows the URB record layout + the UVC payload protocol.

usbmon record (LINKTYPE_USB_LINUX_MMAPPED=220, 64 bytes; =189 is the 48-byte prefix):
  id(8) type(1,'S'/'C'/'E') xfer_type(1,0=iso 1=intr 2=ctrl 3=bulk) epnum(1,bit7=IN)
  devnum(1) busnum(2) flag_setup(1) flag_data(1,'\\0'=data present) ts_sec(8) ts_usec(4)
  status(4) length(4,requested on S / actual on C) len_cap(4,captured incl. iso descs)
  union{setup[8] | iso error_count(4)+numdesc(4)} interval(4) start_frame(4)
  xfer_flags(4) ndesc(4)
ISO data area: ndesc x 16-byte descriptors (status,offset,length,pad) then the URB
buffer copied VERBATIM -- packets sit at their descriptor offsets with real holes
between them, so payloads must be sliced per-descriptor, never read contiguously.
IN data rides completion ('C') records only.

UVC payload header: bHeaderLength, bmHeaderInfo (bit0 FID toggles per frame, bit1 EOF,
bit2 PTS present(+4B), bit3 SCR present(+6B), bit6 ERR), then optional PTS/SCR. A bare
2-byte header with no PTS/SCR is first-class -- that's what thermal cores
(PureThermal/Boson-style) send. Frame boundary = FID toggle or EOF.

A completed frame's timestamp is the pcap timestamp of the URB that carried its FIRST
payload byte: capture-proximal and cadence-stable (last-byte time would add per-frame
transfer-duration jitter), consistent with the repo's SOF/leader-packet stance. ISO
granularity is the URB (a few ms of microframes).
"""
from __future__ import annotations

import logging
import struct
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .pcapio import PcapFormatError, iter_packets, sniff_format

log = logging.getLogger(__name__)

LINKTYPE_USBMON_64 = 220   # LINKTYPE_USB_LINUX_MMAPPED (modern Wireshark/libpcap)
LINKTYPE_USBMON_48 = 189   # LINKTYPE_USB_LINUX (old libpcap)
_USB_LINKTYPES = (LINKTYPE_USBMON_64, LINKTYPE_USBMON_48)

_XFER_ISO, _XFER_INTR, _XFER_CTRL, _XFER_BULK = 0, 1, 2, 3
_XFER_NAME = {_XFER_ISO: "iso", _XFER_INTR: "intr", _XFER_CTRL: "ctrl", _XFER_BULK: "bulk"}
_S, _C, _E = ord("S"), ord("C"), ord("E")

_HDR64 = {"<": struct.Struct("<QBBBBHBBqiiII8siiII"), ">": struct.Struct(">QBBBBHBBqiiII8siiII")}
_HDR48 = {"<": struct.Struct("<QBBBBHBBqiiII8s"), ">": struct.Struct(">QBBBBHBBqiiII8s")}
_ISO_DESC = {"<": struct.Struct("<iIII"), ">": struct.Struct(">iIII")}
_ISO_UNION = {"<": struct.Struct("<ii"), ">": struct.Struct(">ii")}

# UVC bmHeaderInfo bits
_FID, _EOF, _PTS, _SCR, _ERR = 0x01, 0x02, 0x04, 0x08, 0x40

# UVC uncompressed guidFormat -> repo pixel-format string (first 4 bytes are the FourCC)
_GUID_FORMATS = {b"Y16 ": "GRAY16_LE", b"GREY": "GRAY8", b"YUY2": "YUY2",
                 b"NV12": "NV12", b"UYVY": "UYVY", b"I420": "I420"}


@dataclass
class _Urb:
    """One parsed usbmon record (only the fields the extraction/probe paths read)."""
    urb_id: int
    type: int          # _S/_C/_E
    xfer: int
    ep: int            # endpoint number, direction bit stripped
    is_in: bool
    dev: int
    bus: int
    has_setup: bool
    has_data: bool
    status: int
    length: int
    len_cap: int
    setup: bytes
    ndesc: int
    hdr_len: int
    ts_ns: int
    record: bytes

    def data(self) -> bytes:
        """The captured URB buffer (after the iso descriptors, if any), bounded by BOTH
        len_cap (usbmon's own data limit) and the record length (pcap snaplen cut)."""
        start = self.hdr_len + self.ndesc * 16
        end = self.hdr_len + min(self.len_cap, len(self.record) - self.hdr_len)
        return self.record[start:end] if end > start else b""


class _UsbmonParser:
    """Parses usbmon records, locking field endianness on the first record.

    usbmon header fields are raw kernel structs in the CAPTURING host's byte order.
    Essentially every capture is little-endian (x86/ARM); the backstop probes a
    byte-swapped parse when the first records look implausible (busnum > 4096 or
    len_cap overrunning the record), which covers a BE-host capture read anywhere.
    """

    def __init__(self) -> None:
        self._endian: Optional[str] = None

    def _parse_with(self, endian: str, linktype: int, ts_ns: int, rec: bytes) -> Optional[_Urb]:
        hdr_len = 64 if linktype == LINKTYPE_USBMON_64 else 48
        if len(rec) < hdr_len:
            return None
        if hdr_len == 64:
            f = _HDR64[endian].unpack_from(rec)
            ndesc = f[17]
        else:
            f = _HDR48[endian].unpack_from(rec)
            ndesc = 0
        urb = _Urb(
            urb_id=f[0], type=f[1], xfer=f[2], ep=f[3] & 0x7F, is_in=bool(f[3] & 0x80),
            dev=f[4], bus=f[5], has_setup=(f[6] == 0), has_data=(f[7] == 0),
            status=f[10], length=f[11], len_cap=f[12], setup=f[13],
            ndesc=0, hdr_len=hdr_len, ts_ns=ts_ns, record=rec)
        if urb.xfer == _XFER_ISO:
            if hdr_len == 64:
                urb.ndesc = min(ndesc, 128)
            else:
                # 48-byte header has no ndesc field; the union's numdesc is the URB's full
                # (unclamped) packet count -- sanity-clamp to what the record can hold.
                numdesc = _ISO_UNION[endian].unpack(urb.setup)[1]
                urb.ndesc = max(0, min(numdesc, (len(rec) - hdr_len) // 16, 128))
        return urb

    @staticmethod
    def _plausible(urb: _Urb) -> bool:
        return (urb.type in (_S, _C, _E) and urb.xfer <= 3 and urb.bus <= 4096
                and urb.len_cap <= len(urb.record) - urb.hdr_len + 16 * 128)

    def parse(self, linktype: int, ts_ns: int, rec: bytes) -> Optional[_Urb]:
        if self._endian is not None:
            return self._parse_with(self._endian, linktype, ts_ns, rec)
        # Endianness not locked yet. A record can be AMBIGUOUS (e.g. len_cap=0 parses
        # sanely both ways), so lock only when exactly one byte order is plausible;
        # until then prefer the parse with the smaller busnum (the multi-byte field
        # a swap inflates: bus 1 misread is 256).
        le = self._parse_with("<", linktype, ts_ns, rec)
        be = self._parse_with(">", linktype, ts_ns, rec)
        le_ok = le is not None and self._plausible(le)
        be_ok = be is not None and self._plausible(be)
        if le_ok and not be_ok:
            self._endian = "<"
            return le
        if be_ok and not le_ok:
            log.warning("usbmon fields parse as big-endian (capture from a BE host?)")
            self._endian = ">"
            return be
        if le_ok and be_ok:
            return le if le.bus <= be.bus else be
        return le   # neither plausible: let downstream filters discard it

    def iso_payloads(self, urb: _Urb) -> Iterator[Tuple[bytes, bool]]:
        """(payload_bytes, lost) per iso packet of a completion URB. `lost` = this packet's bytes are
        not in the capture -- cut by len_cap/snaplen, OR an errored/missed packet (nonzero status):
        either way the frame being assembled has a hole and must be skipped. (A raw frame with a hole
        is also caught by the size check; an MJPEG one is not -- a JPEG with a hole still starts with
        SOI and ends with EOI, so it would be stream-copied as valid.)"""
        endian = self._endian or "<"
        data = urb.data()
        for i in range(urb.ndesc):
            off = urb.hdr_len + i * 16
            if off + 16 > len(urb.record):
                return
            iso_status, iso_off, iso_len, _pad = _ISO_DESC[endian].unpack_from(urb.record, off)
            if iso_status != 0:
                yield b"", True    # missed/errored: its bytes are gone -> poison the current frame
                continue
            if iso_len == 0:
                continue           # empty packet: nothing was sent, so nothing is missing
            if iso_off + iso_len > len(data):
                yield b"", True    # cut by len_cap/snaplen: poison the current frame
            else:
                yield data[iso_off:iso_off + iso_len], False


# ---- probe -----------------------------------------------------------------

@dataclass
class Negotiated:
    """From the capture's VS_PROBE/COMMIT control traffic."""
    fps: Optional[float] = None
    max_video_frame_size: Optional[int] = None
    max_payload_size: Optional[int] = None
    format_index: Optional[int] = None
    frame_index: Optional[int] = None


@dataclass
class StreamInfo:
    bus: int
    dev: int
    ep: int
    xfer: str                  # "iso" | "bulk" | "intr"
    urbs: int = 0
    payload_bytes: int = 0
    uvc_score: float = 0.0
    negotiated: Optional[Negotiated] = None                       # per-device, attached to its streams
    described: Optional[Tuple[str, int, int]] = None              # (pixel_format, w, h) if enumeration seen

    def describe(self) -> str:
        s = (f"bus {self.bus} dev {self.dev} ep 0x{self.ep | 0x80:02x} {self.xfer} "
             f"{self.payload_bytes / 1e6:.1f} MB in {self.urbs} URBs (uvc score {self.uvc_score:.2f})")
        if self.described:
            fmt, w, h = self.described
            s += f" [{fmt} {w}x{h}]"
        if self.negotiated and self.negotiated.max_video_frame_size:
            s += f" [commit: frame<= {self.negotiated.max_video_frame_size}B" + \
                 (f" @{self.negotiated.fps:.1f}fps]" if self.negotiated.fps else "]")
        return s


@dataclass
class ProbeResult:
    path: str
    file_format: str
    linktypes: set = field(default_factory=set)
    packets: int = 0
    duration_ns: int = 0
    streams: List[StreamInfo] = field(default_factory=list)
    best: Optional[StreamInfo] = None

    def describe_streams(self) -> str:
        if not self.streams:
            return "none"
        return "; ".join(s.describe() for s in self.streams)


class _ScoreState:
    """UVC-plausibility sampling for one candidate stream (first ~200 non-empty payloads)."""

    def __init__(self) -> None:
        self.sampled = 0
        self.plausible = 0
        self.fids = set()

    def feed(self, payload: bytes) -> None:
        if self.sampled >= 200 or not payload:
            return
        self.sampled += 1
        if _parse_uvc_header(payload) is not None:
            self.plausible += 1
            self.fids.add(payload[1] & _FID)

    @property
    def score(self) -> float:
        if not self.sampled:
            return 0.0
        frac = self.plausible / self.sampled
        return frac if len(self.fids) > 1 else frac * 0.5   # a real stream's FID toggles


def _parse_uvc_header(payload: bytes, strict: bool = False) -> Optional[Tuple[int, int]]:
    """(bHeaderLength, bmHeaderInfo) if the payload starts with a sane UVC header, else None.
    Tolerant by default: padded headers are accepted, EOH is not required, and a bare
    2-byte header with no PTS/SCR (the thermal-core shape) is first-class."""
    if len(payload) < 2:
        return None
    hlen, info = payload[0], payload[1]
    implied = 2 + (4 if info & _PTS else 0) + (6 if info & _SCR else 0)
    if strict:
        if hlen != implied or hlen > len(payload):
            return None
    elif not implied <= hlen <= len(payload):
        return None
    return hlen, info


def _walk_config_descriptor(data: bytes, formats: dict, frames: dict) -> None:
    """Collect VS format/frame descriptors from a configuration-descriptor blob."""
    i, vs_iface, cur_fmt = 0, False, None
    while i + 2 <= len(data):
        blen, btype = data[i], data[i + 1]
        if blen < 2 or i + blen > len(data):
            return
        d = data[i:i + blen]
        if btype == 0x04 and blen >= 8:                 # INTERFACE: class 0x0E sub 2 = VideoStreaming
            vs_iface = (d[5] == 0x0E and d[6] == 0x02)
        elif btype == 0x24 and vs_iface and blen >= 4:  # CS_INTERFACE within VideoStreaming
            sub = d[2]
            if sub == 0x04 and blen >= 21:              # VS_FORMAT_UNCOMPRESSED
                cur_fmt = d[3]
                formats[cur_fmt] = _GUID_FORMATS.get(d[5:9], None)
            elif sub == 0x06:                           # VS_FORMAT_MJPEG
                cur_fmt = d[3]
                formats[cur_fmt] = "MJPEG"
            elif sub in (0x05, 0x07) and blen >= 9 and cur_fmt is not None:
                # VS_FRAME_{UNCOMPRESSED,MJPEG}: frames follow their format descriptor
                w = d[5] | (d[6] << 8)
                h = d[7] | (d[8] << 8)
                frames[(cur_fmt, d[3])] = (w, h)
        i += blen


def probe(path: str, max_packets: Optional[int] = None) -> ProbeResult:
    """One streaming pass: candidate video streams (ranked), negotiated format info,
    and -- when enumeration was captured -- the described (pixel_format, w, h)."""
    res = ProbeResult(path=path, file_format=sniff_format(path))
    parser = _UsbmonParser()
    stats: Dict[Tuple[int, int, int, int], StreamInfo] = {}
    scores: Dict[Tuple[int, int, int, int], _ScoreState] = {}
    pending_setup: Dict[Tuple[int, int], bytes] = {}       # (bus, urb_id) -> setup, for control IN
    negotiated: Dict[Tuple[int, int], Negotiated] = {}     # (bus, dev)
    committed: Dict[Tuple[int, int], Negotiated] = {}
    descriptors: Dict[Tuple[int, int], Tuple[dict, dict]] = {}   # (bus, dev) -> (formats, frames)
    first_ts = last_ts = None

    def _vs_ctrl(neg_map, bus, dev, data):
        if len(data) < 26:
            return
        neg = neg_map.setdefault((bus, dev), Negotiated())
        neg.format_index, neg.frame_index = data[2], data[3]
        interval = struct.unpack_from("<I", data, 4)[0]        # 100 ns units
        neg.fps = 1e7 / interval if interval else None
        neg.max_video_frame_size = struct.unpack_from("<I", data, 18)[0]
        neg.max_payload_size = struct.unpack_from("<I", data, 22)[0]

    for n, (linktype, ts_ns, rec) in enumerate(iter_packets(path)):
        if max_packets is not None and n >= max_packets:
            break
        res.packets += 1
        res.linktypes.add(linktype)
        if linktype not in _USB_LINKTYPES:
            continue
        urb = parser.parse(linktype, ts_ns, rec)
        if urb is None:
            continue
        first_ts = ts_ns if first_ts is None else first_ts
        last_ts = ts_ns

        if urb.xfer == _XFER_CTRL:
            # UVC negotiation + (if enumeration was captured) descriptors, per device.
            if urb.type == _S and urb.has_setup:
                bm, breq, w_value = urb.setup[0], urb.setup[1], urb.setup[2] | (urb.setup[3] << 8)
                if bm == 0x21 and breq == 0x01 and (w_value >> 8) in (1, 2) and urb.has_data:
                    # SET_CUR of VS_PROBE(1)/VS_COMMIT(2): data stage rides the same 'S'
                    _vs_ctrl(committed if (w_value >> 8) == 2 else negotiated,
                             urb.bus, urb.dev, urb.data())
                elif bm & 0x80:                       # device-to-host: response data on the 'C'
                    pending_setup[(urb.bus, urb.urb_id)] = urb.setup
            elif urb.type == _C and urb.has_data:
                setup = pending_setup.pop((urb.bus, urb.urb_id), None)
                if setup is not None:
                    bm, breq, w_value = setup[0], setup[1], setup[2] | (setup[3] << 8)
                    if bm == 0xA1 and breq == 0x81 and (w_value >> 8) in (1, 2):
                        _vs_ctrl(negotiated, urb.bus, urb.dev, urb.data())    # GET_CUR response
                    elif bm == 0x80 and breq == 0x06 and (w_value >> 8) == 0x02:
                        fmts, frms = descriptors.setdefault((urb.bus, urb.dev), ({}, {}))
                        _walk_config_descriptor(urb.data(), fmts, frms)       # GET_DESCRIPTOR(CONFIG)
            continue

        if urb.type != _C or not urb.is_in or urb.xfer not in (_XFER_ISO, _XFER_BULK, _XFER_INTR):
            continue
        key = (urb.bus, urb.dev, urb.ep, urb.xfer)
        info = stats.get(key)
        if info is None:
            info = stats[key] = StreamInfo(bus=urb.bus, dev=urb.dev, ep=urb.ep,
                                           xfer=_XFER_NAME[urb.xfer])
            scores[key] = _ScoreState()
        info.urbs += 1
        if urb.xfer == _XFER_ISO:
            for payload, truncated in parser.iso_payloads(urb):
                if not truncated:
                    info.payload_bytes += len(payload)
                    scores[key].feed(payload)
        elif urb.has_data:
            data = urb.data()
            info.payload_bytes += len(data)
            if urb.xfer == _XFER_BULK:
                scores[key].feed(data)

    if not res.linktypes & set(_USB_LINKTYPES):
        seen = ", ".join(str(lt) for lt in sorted(res.linktypes)) or "none"
        raise PcapFormatError(
            f"{path}: no Linux usbmon packets (link types seen: {seen}) -- this is not a USB "
            f"capture; record with Wireshark/tcpdump on the usbmonX interface (modprobe usbmon)")

    res.duration_ns = (last_ts - first_ts) if first_ts is not None else 0
    for key, info in stats.items():
        info.uvc_score = scores[key].score
        neg = committed.get((info.bus, info.dev)) or negotiated.get((info.bus, info.dev))
        info.negotiated = neg
        desc = descriptors.get((info.bus, info.dev))
        if neg and desc and neg.format_index in desc[0]:
            fmt = desc[0][neg.format_index]
            wh = desc[1].get((neg.format_index, neg.frame_index))
            if fmt and wh:
                info.described = (fmt, wh[0], wh[1])
    res.streams = sorted(stats.values(), key=lambda s: -s.payload_bytes)
    candidates = [s for s in res.streams if s.xfer in ("iso", "bulk") and s.uvc_score >= 0.9]
    res.best = candidates[0] if candidates else None
    return res


# ---- frame extraction ------------------------------------------------------

@dataclass
class ExtractorStats:
    frames_ok: int = 0
    err_frames: int = 0
    truncated: int = 0
    bad_header: int = 0
    trailing: int = 0
    size_drops: int = 0
    sizes_seen: Counter = field(default_factory=Counter)   # wrong sizes observed (raw mode)
    urbs: int = 0
    first_ts_ns: Optional[int] = None
    last_ts_ns: Optional[int] = None


class UvcFrameExtractor:
    """Reassembles UVC frames from one capture pass; iterate to get (ts_ns, frame_bytes).

    Streaming: at most one frame (+ one pcap record) in RAM. Stateless across passes --
    two iterations over the same file yield identical sequences (loop replay re-parses).
    """

    # frame-assembly states (initial = AWAIT with no FID: the first payload begins a
    # frame; if the capture started mid-frame, finalize-validation rejects the partial)
    _ACCUM, _AWAIT, _SKIP = range(3)

    def __init__(self, path: str, *, bus: int, dev: int, ep: int,
                 expected_frame_size: Optional[int] = None, mjpeg: bool = False,
                 max_payload_size: Optional[int] = None, strict: bool = False) -> None:
        self.path = path
        self.bus, self.dev, self.ep = bus, dev, ep & 0x7F
        self.expected_size = expected_frame_size
        self.mjpeg = mjpeg
        self.max_payload = max_payload_size or 0
        self.strict = strict
        self.stats = ExtractorStats()

    # -- state machine --------------------------------------------------------
    def _reset(self) -> None:
        self._state = self._AWAIT
        self._fid: Optional[int] = None      # the current (ACCUM) / last finalized (AWAIT) FID
        self._buf = bytearray()
        self._start_ts = 0
        self._bulk_cont = False              # mid-payload: next bulk URB is continuation bytes
        self._pending = bytearray()          # the bulk payload being assembled (header + data)
        self._pending_ts = 0
        self._req_len: Dict[Tuple[int, int], int] = {}   # (bus,id) -> requested length ('S')

    def _finalize(self, out: List[Tuple[int, bytes]]) -> None:
        buf = bytes(self._buf)
        self._buf = bytearray()
        if self.mjpeg:
            trimmed = buf.rstrip(b"\x00")
            if trimmed.startswith(b"\xff\xd8") and trimmed.endswith(b"\xff\xd9"):
                self.stats.frames_ok += 1
                out.append((self._start_ts, trimmed))
            else:
                self.stats.size_drops += 1
                self.stats.sizes_seen[len(trimmed)] += 1
        elif self.expected_size is None or len(buf) == self.expected_size:
            self.stats.frames_ok += 1
            out.append((self._start_ts, buf))
        else:
            self.stats.size_drops += 1
            self.stats.sizes_seen[len(buf)] += 1

    def _feed(self, payload: bytes, ts_ns: int, out: List[Tuple[int, bytes]]) -> None:
        hdr = _parse_uvc_header(payload, self.strict)
        if hdr is None:
            self.stats.bad_header += 1
            if self._state == self._ACCUM:
                self._buf = bytearray()
                self._state = self._SKIP
            return
        hlen, info = hdr
        fid, eof, err = info & _FID, bool(info & _EOF), bool(info & _ERR)
        data = payload[hlen:]

        if self._state == self._AWAIT:
            if fid == self._fid and not data:
                self.stats.trailing += 1     # post-EOF header-only keepalives, finished FID
                return
            # A DATA payload with the finished FID is a new frame, not a keepalive: after a
            # wholly-lost frame (usbmon ring overflow) the next frame legitimately repeats
            # the FID, and treating it as trailing would silently discard an intact frame.
            self._begin(fid, ts_ns)
        elif self._state == self._SKIP:
            if fid == self._fid:
                return                       # still inside the poisoned frame
            self._begin(fid, ts_ns)

        # ACCUM
        if err:
            self.stats.err_frames += 1
            self._buf = bytearray()
            self._state = self._SKIP
            return
        if fid != self._fid:
            # EOF was lost: the toggle itself is the boundary. Finalize the old frame
            # (size validation rejects it if payloads were lost) and start the new one.
            self._finalize(out)
            self._begin(fid, ts_ns)
        self._buf += data
        if eof:
            self._finalize(out)
            self._state = self._AWAIT

    def _begin(self, fid: int, ts_ns: int) -> None:
        self._state = self._ACCUM
        self._fid = fid
        self._start_ts = ts_ns
        self._buf = bytearray()

    # -- capture walk ---------------------------------------------------------
    def __iter__(self) -> Iterator[Tuple[int, bytes]]:
        self._reset()
        self.stats = ExtractorStats()   # per-pass: loop replay re-iterates the same extractor
        parser = _UsbmonParser()
        out: List[Tuple[int, bytes]] = []
        for linktype, ts_ns, rec in iter_packets(self.path):
            if linktype not in _USB_LINKTYPES:
                continue
            urb = parser.parse(linktype, ts_ns, rec)
            if urb is None or urb.dev != self.dev or urb.bus != self.bus:
                continue
            if not urb.is_in or urb.ep != self.ep:
                continue
            if urb.type == _S:
                if urb.xfer == _XFER_BULK:
                    self._req_len[(urb.bus, urb.urb_id)] = urb.length
                continue
            if urb.type != _C:
                continue
            self.stats.urbs += 1
            if self.stats.first_ts_ns is None:   # not `or`: a relative-time capture starts at ts 0
                self.stats.first_ts_ns = ts_ns
            self.stats.last_ts_ns = ts_ns
            if urb.xfer == _XFER_ISO:
                for payload, truncated in parser.iso_payloads(urb):
                    if truncated:
                        self.stats.truncated += 1
                        if self._state == self._ACCUM:
                            self._buf = bytearray()
                            self._state = self._SKIP
                    else:
                        self._feed(payload, ts_ns, out)
            elif urb.xfer == _XFER_BULK:
                self._feed_bulk(urb, ts_ns, out)
            for item in out:
                yield item
            out.clear()
        self._end_of_capture()

    def _feed_bulk(self, urb: _Urb, ts_ns: int, out: List[Tuple[int, bytes]]) -> None:
        """Bulk payloads can span URBs (the UVC header appears once per PAYLOAD, and its
        EOF describes the whole payload) -- so assemble the complete payload first, then
        feed it. A payload ends on a SHORT URB, on reaching dwMaxPayloadTransferSize, or
        -- with no 'S' captured to detect shortness -- at every URB (the Boson-style
        one-payload-per-URB case parses identically either way)."""
        if urb.length == 0:
            # ZLP: terminates a payload whose length was an exact multiple of the URB size
            self._req_len.pop((urb.bus, urb.urb_id), None)
            if self._bulk_cont and self._pending:
                self._feed(bytes(self._pending), self._pending_ts, out)
            self._pending = bytearray()
            self._bulk_cont = False
            return
        if not urb.has_data:
            # bytes existed but weren't captured (usbmon data limit): the payload has a
            # hole -- poison rather than silently merging around it
            self.stats.truncated += 1
            if self._state == self._ACCUM:
                self._buf = bytearray()
                self._state = self._SKIP
            self._pending = bytearray()
            self._bulk_cont = False
            self._req_len.pop((urb.bus, urb.urb_id), None)
            return
        data = urb.data()
        if len(data) < urb.length:
            # snaplen/usbmon cut the record short of the URB's actual bytes: the frame
            # can never validate; poison it rather than assembling a hole.
            self.stats.truncated += 1
            if self._state == self._ACCUM:
                self._buf = bytearray()
                self._state = self._SKIP
            self._bulk_cont = False
            self._pending = bytearray()
            return
        req = self._req_len.pop((urb.bus, urb.urb_id), None)
        short = req is not None and urb.length < req
        if not self._bulk_cont:
            self._pending = bytearray()
            self._pending_ts = ts_ns
        self._pending += data
        if len(self._pending) > 64 * 1024 * 1024:
            # no short URB and no max-payload bound in sight: never assemble unbounded
            self.stats.truncated += 1
            self._pending = bytearray()
            self._bulk_cont = False
            return
        while self.max_payload and len(self._pending) >= self.max_payload:
            self._feed(bytes(self._pending[:self.max_payload]), self._pending_ts, out)
            del self._pending[:self.max_payload]
            self._pending_ts = ts_ns
        if short or req is None:
            if self._pending:
                self._feed(bytes(self._pending), self._pending_ts, out)
            self._pending = bytearray()
            self._bulk_cont = False
        else:
            self._bulk_cont = len(self._pending) > 0

    def _end_of_capture(self) -> None:
        st = self.stats
        if st.frames_ok:
            return
        if st.urbs == 0:
            raise ValueError(
                f"{self.path}: no IN data on bus {self.bus} dev {self.dev} ep 0x{self.ep | 0x80:02x} "
                f"-- wrong endpoint selection? Run with -v for the probe's stream table")
        if st.size_drops and st.sizes_seen:
            modal, count = st.sizes_seen.most_common(1)[0]
            raise ValueError(
                f"{self.path}: {st.size_drops} reassembled frame(s) but none matched the expected "
                f"{self.expected_size} bytes (most common size: {modal}, x{count}) -- "
                f"check pcap.width/height/pixel_format against what the camera actually streamed")
        raise ValueError(
            f"{self.path}: {st.urbs} URBs on the selected endpoint but no UVC frames reassembled "
            f"(bad_header={st.bad_header} truncated={st.truncated} err={st.err_frames}) -- "
            f"is this endpoint really the video stream?")


def select_stream(pr: ProbeResult, bus: Optional[int] = None, dev: Optional[int] = None,
                  ep: Optional[int] = None) -> StreamInfo:
    """Resolve the video stream from a probe: unpinned = the best UVC-looking candidate;
    (partially) pinned = the busiest stream matching the pins. Legible errors either way."""
    pinned = {k: v for k, v in (("bus", bus), ("device", dev), ("endpoint", ep))
              if v is not None}
    if pinned:
        match = [s for s in pr.streams if s.xfer in ("iso", "bulk")
                 and (bus is None or s.bus == bus) and (dev is None or s.dev == dev)
                 and (ep is None or s.ep == (ep & 0x7F))]
        if not match:
            raise ValueError(
                f"{pr.path}: no IN stream matches the pinned {pinned}; "
                f"streams seen: {pr.describe_streams()}")
        return max(match, key=lambda s: s.payload_bytes)
    if pr.best is None:
        raise ValueError(
            f"{pr.path}: no UVC-looking IN stream found; IN endpoints seen: {pr.describe_streams()}")
    return pr.best


def iter_frames(path: str, *, bus: Optional[int] = None, dev: Optional[int] = None,
                ep: Optional[int] = None, expected_frame_size: Optional[int] = None,
                mjpeg: bool = False, max_payload_size: Optional[int] = None,
                strict: bool = False) -> Iterator[Tuple[int, bytes]]:
    """Yield (capture_ts_ns, frame_bytes) for the capture's video stream, in file order.

    With bus/dev/ep unset, a probe pass selects the stream first (two sequential passes,
    still O(1) memory). Callers that loop should probe() once and pass the pin down.
    """
    if bus is None or dev is None or ep is None:
        best = select_stream(probe(path), bus, dev, ep)
        log.info("pcap stream selected: %s", best.describe())
        bus, dev, ep = best.bus, best.dev, best.ep
        if max_payload_size is None and best.negotiated:
            max_payload_size = best.negotiated.max_payload_size
    return iter(UvcFrameExtractor(
        path, bus=bus, dev=dev, ep=ep, expected_frame_size=expected_frame_size,
        mjpeg=mjpeg, max_payload_size=max_payload_size, strict=strict))
