"""Synthetic usbmon capture builder for the uvcpcap tests (pure stdlib; no GStreamer).

Builds byte-exact pcap / pcap-ns / pcapng / linktype-189 captures of a fake UVC camera
so the parser round-trip can be asserted exactly: known ramp frames split across
realistic ISO URBs (FID toggles, EOF, descriptor HOLES filled with 0xEE so any
contiguous-read bug corrupts frames and fails the assertions), interleaved non-video
noise, VS_PROBE/COMMIT control traffic, optional enumeration descriptors, one ERR-bit
frame, and one snaplen-truncated URB.

Builders return (capture_bytes, expected) where expected is the exact
[(ts_ns, frame_bytes)] list iter_frames must yield.
"""
from __future__ import annotations

import base64
import io
import struct
from typing import List, Optional, Tuple

Y16_GUID = b"Y16 " + bytes((0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))

# A REAL (decodable) 32x24 GRAY8 JPEG (jpegenc quality=30), 373 bytes. The MJPEG frames
# must be valid JPEGs, not just SOI...EOI blobs: gstreamer's jpegparse PARSES the segment
# structure and silently discards garbage, which would wedge the pcap source's decode
# pipeline in the e2e test while the pure-python round-trip stayed green.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQ"
    "c1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/wAALCAAYACABAREA/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcI"
    "CQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcY"
    "GRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKj"
    "pKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AMf7"
    "P7UfZ/aj7P7UfZ/atb7P7UfZ/aj7P7UfZ/atX7P7UfZ/aj7P7UfZ/av/2Q==")


def make_jpeg(k: int, extra: int = 0) -> bytes:
    """A valid JPEG unique to frame k: TINY_JPEG with a COM segment (varying content and
    size) spliced in right after SOI -- decodable by jpegdec, byte-distinct per frame."""
    comment = bytes((37 * (k + 1) + i) % 251 for i in range(40 + extra))
    com = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    return TINY_JPEG[:2] + com + TINY_JPEG[2:]

_ISO, _INTR, _CTRL, _BULK = 0, 1, 2, 3


# ---- container writers -----------------------------------------------------

class PcapWriter:
    def __init__(self, linktype: int = 220, nanosecond: bool = False, big_endian: bool = False):
        self.e = ">" if big_endian else "<"
        self.ns = nanosecond
        self.buf = io.BytesIO()
        magic = 0xA1B23C4D if nanosecond else 0xA1B2C3D4
        self.buf.write(struct.pack(self.e + "IHHiIII", magic, 2, 4, 0, 0, 262144, linktype))

    def add(self, ts_ns: int, data: bytes) -> None:
        frac = ts_ns % 1_000_000_000 if self.ns else (ts_ns % 1_000_000_000) // 1000
        self.buf.write(struct.pack(self.e + "IIII", ts_ns // 1_000_000_000, frac,
                                   len(data), len(data)))
        self.buf.write(data)

    def getvalue(self) -> bytes:
        return self.buf.getvalue()


class PcapngWriter:
    def __init__(self, big_endian: bool = False):
        self.e = ">" if big_endian else "<"
        self.buf = io.BytesIO()
        self.n_ifaces = 0
        self.add_shb()

    def _block(self, btype: int, body: bytes) -> None:
        pad = (-len(body)) % 4
        total = 12 + len(body) + pad
        self.buf.write(struct.pack(self.e + "II", btype, total))
        self.buf.write(body + b"\x00" * pad)
        self.buf.write(struct.pack(self.e + "I", total))

    def add_shb(self) -> None:
        body = struct.pack(self.e + "IHHq", 0x1A2B3C4D, 1, 0, -1)
        self._block(0x0A0D0D0A, body)
        self.n_ifaces = 0

    def add_idb(self, linktype: int, tsresol: Optional[int] = 9) -> int:
        body = struct.pack(self.e + "HHI", linktype, 0, 262144)
        if tsresol is not None:
            body += struct.pack(self.e + "HH", 9, 1) + bytes((tsresol,)) + b"\x00" * 3
        body += struct.pack(self.e + "HH", 0, 0)   # opt_endofopt
        self._block(0x00000001, body)
        self.n_ifaces += 1
        return self.n_ifaces - 1

    def add_epb(self, ifid: int, ts_ns: int, data: bytes, tsresol: int = 9) -> None:
        if tsresol & 0x80:
            ts = (ts_ns << (tsresol & 0x7F)) // 1_000_000_000
        else:
            ts = ts_ns // (10 ** (9 - tsresol)) if tsresol <= 9 else ts_ns * (10 ** (tsresol - 9))
        body = struct.pack(self.e + "IIIII", ifid, ts >> 32, ts & 0xFFFFFFFF,
                           len(data), len(data))
        self._block(0x00000006, body + data)

    def getvalue(self) -> bytes:
        return self.buf.getvalue()


# ---- usbmon record / UVC payload packers -----------------------------------

def usbmon_record(*, urb_id: int, rtype: str, xfer: int, ep: int, dev: int, bus: int = 1,
                  status: int = 0, length: int = 0, len_cap: Optional[int] = None,
                  setup: bytes = b"\x2d" * 8, flag_setup: int = ord("-"),
                  flag_data: int = 0, iso_descs: Optional[List[Tuple[int, int, int]]] = None,
                  data: bytes = b"", linktype: int = 220, big_endian: bool = False) -> bytes:
    """One usbmon record: header (+ iso descriptor array) + data region."""
    e = ">" if big_endian else "<"
    descs = iso_descs or []
    desc_bytes = b"".join(struct.pack(e + "iIII", s, o, ln, 0) for s, o, ln in descs)
    if len_cap is None:
        len_cap = len(desc_bytes) + len(data)
    if xfer == _ISO:
        union = struct.pack(e + "ii", 0, len(descs))
    else:
        union = setup
    hdr = struct.pack(e + "QBBBBHBBqiiII8s", urb_id, ord(rtype), xfer, ep, dev, bus,
                      flag_setup, flag_data, 0, 0, status, length, len_cap, union)
    if linktype == 220:
        hdr += struct.pack(e + "iiII", 0, 0, 0, len(descs))
    return hdr + desc_bytes + data


def uvc_payload(data: bytes, *, fid: int, eof: bool = False, err: bool = False,
                pts: Optional[int] = None, scr: Optional[Tuple[int, int]] = None,
                hlen: Optional[int] = None) -> bytes:
    info = (0x01 if fid else 0) | (0x02 if eof else 0) | (0x40 if err else 0)
    body = b""
    if pts is not None:
        info |= 0x04
        body += struct.pack("<I", pts)
    if scr is not None:
        info |= 0x08
        body += struct.pack("<IH", scr[0], scr[1])
    header = bytes((2 + len(body) if hlen is None else hlen, info)) + body
    if hlen is not None and hlen > len(header):
        header += b"\x00" * (hlen - len(header))   # firmware-padded header
    return header + data


def iso_urb(payloads: List[bytes], *, urb_id: int, ep: int, dev: int, bus: int = 1,
            slot_len: int, linktype: int = 220, big_endian: bool = False,
            truncate_at: Optional[int] = None) -> bytes:
    """A 'C' ISO URB carrying `payloads` at slot offsets, holes filled with 0xEE.
    truncate_at cuts the RECORD (simulating snaplen) while len_cap still claims it all."""
    region = bytearray(b"\xee" * (slot_len * len(payloads)))
    descs = []
    for i, p in enumerate(payloads):
        assert len(p) <= slot_len
        region[i * slot_len:i * slot_len + len(p)] = p
        descs.append((0, i * slot_len, len(p)))
    rec = usbmon_record(urb_id=urb_id, rtype="C", xfer=_ISO, ep=0x80 | ep, dev=dev, bus=bus,
                        length=sum(len(p) for p in payloads), iso_descs=descs,
                        data=bytes(region), linktype=linktype, big_endian=big_endian)
    return rec[:truncate_at] if truncate_at is not None else rec


def probe_commit_block(*, fmt_idx: int = 1, frame_idx: int = 1, interval_100ns: int = 166666,
                       max_frame: int = 0, max_payload: int = 0) -> bytes:
    b = bytearray(26)
    b[2], b[3] = fmt_idx, frame_idx
    struct.pack_into("<I", b, 4, interval_100ns)
    struct.pack_into("<I", b, 18, max_frame)
    struct.pack_into("<I", b, 22, max_payload)
    return bytes(b)


def config_descriptor_blob(*, width: int, height: int, guid: bytes = Y16_GUID,
                           bpp: int = 16, mjpeg: bool = False) -> bytes:
    """Minimal CONFIGURATION blob: config + VS interface + one format + one frame."""
    cfg = struct.pack("<BBHBBBBB", 9, 2, 0, 2, 1, 0, 0x80, 250)
    iface = bytes((9, 4, 1, 0, 1, 0x0E, 0x02, 0, 0))
    if mjpeg:
        fmt = bytes((11, 0x24, 0x06, 1, 1, 0, 0, 0, 0, 0, 0))
    else:
        fmt = bytes((27, 0x24, 0x04, 1, 1)) + guid + bytes((bpp, 1, 0, 0, 0, 0))
    frame = bytearray(30)
    frame[0:5] = bytes((30, 0x24, 0x07 if mjpeg else 0x05, 1, 0))
    struct.pack_into("<HH", frame, 5, width, height)
    struct.pack_into("<I", frame, 21, 166666)   # dwDefaultFrameInterval
    frame[25] = 1
    struct.pack_into("<I", frame, 26, 166666)
    return cfg + iface + fmt + bytes(frame)


# ---- scenario builders -----------------------------------------------------

def ramp_frame_y16(k: int, width: int, height: int) -> bytes:
    return b"".join(struct.pack("<H", (i + 251 * k) & 0xFFFF) for i in range(width * height))


class _Capture:
    """Dispatches records into the requested container format."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.linktype = 189 if fmt == "linktype189" else 220
        self.big_endian = fmt.endswith("-be")
        if fmt.startswith("pcapng"):
            self.w = PcapngWriter(big_endian=self.big_endian)
            self.tsresol = 6 if fmt == "pcapng-us" else 9
            self.ifid = self.w.add_idb(self.linktype, tsresol=self.tsresol)
        else:
            self.w = PcapWriter(linktype=self.linktype, nanosecond=(fmt == "pcap-ns"),
                                big_endian=self.big_endian)
            self.tsresol = 9 if fmt == "pcap-ns" else 6

    def quant(self, ts_ns: int) -> int:
        q = 10 ** (9 - self.tsresol)
        return ts_ns // q * q

    def add(self, ts_ns: int, rec: bytes) -> None:
        if isinstance(self.w, PcapngWriter):
            self.w.add_epb(self.ifid, ts_ns, rec, tsresol=self.tsresol)
        else:
            self.w.add(ts_ns, rec)

    def getvalue(self) -> bytes:
        return self.w.getvalue()


def _noise(cap: _Capture, ts: int, dev: int, urb_id: int) -> None:
    """Non-video chatter: a HID interrupt IN report + hub control traffic."""
    cap.add(ts, usbmon_record(urb_id=urb_id, rtype="C", xfer=_INTR, ep=0x81, dev=dev,
                              length=8, data=b"\x01" * 8,
                              linktype=cap.linktype, big_endian=cap.big_endian))
    cap.add(ts + 1000, usbmon_record(
        urb_id=urb_id + 1, rtype="C", xfer=_CTRL, ep=0x80, dev=dev + 1, length=4,
        data=b"\x00\x01\x00\x00", linktype=cap.linktype, big_endian=cap.big_endian))


def build_y16_capture(*, frames: int = 6, width: int = 64, height: int = 8,
                      fmt: str = "pcap", bus: int = 1, dev: int = 5, ep: int = 1,
                      base_ns: int = 1_700_000_000_000_000_000,
                      frame_period_ns: int = 16_666_000,
                      payload_data: int = 192, pkts_per_urb: int = 4,
                      err_frame: Optional[int] = 3, truncated_frame: Optional[int] = 4,
                      omit_frame: Optional[int] = None,   # frame entirely absent (ring overflow)
                      include_enumeration: bool = True, include_commit: bool = True,
                      leading_partial: bool = True, hlen: Optional[int] = None,
                      ) -> Tuple[bytes, List[Tuple[int, bytes]]]:
    """ISO Y16 capture; returns (capture_bytes, expected [(ts_ns, frame_bytes)])."""
    cap = _Capture(fmt)
    expected: List[Tuple[int, bytes]] = []
    urb_id = 0xFFFF880000000000
    frame_size = width * height * 2

    if include_enumeration:
        setup = bytes((0x80, 0x06, 0x00, 0x02, 0x00, 0x00, 0xFF, 0x00))
        cap.add(cap.quant(base_ns - 5_000_000), usbmon_record(
            urb_id=urb_id, rtype="S", xfer=_CTRL, ep=0x80, dev=dev, bus=bus,
            setup=setup, flag_setup=0, flag_data=ord("<"), length=255,
            linktype=cap.linktype, big_endian=cap.big_endian))
        cap.add(cap.quant(base_ns - 4_900_000), usbmon_record(
            urb_id=urb_id, rtype="C", xfer=_CTRL, ep=0x80, dev=dev, bus=bus,
            data=config_descriptor_blob(width=width, height=height),
            length=255, linktype=cap.linktype, big_endian=cap.big_endian))
        urb_id += 2
    if include_commit:
        setup = bytes((0x21, 0x01, 0x00, 0x02, 0x01, 0x00, 26, 0x00))   # SET_CUR VS_COMMIT
        cap.add(cap.quant(base_ns - 2_000_000), usbmon_record(
            urb_id=urb_id, rtype="S", xfer=_CTRL, ep=0x00, dev=dev, bus=bus,
            setup=setup, flag_setup=0, flag_data=0, length=26,
            data=probe_commit_block(max_frame=frame_size,
                                    max_payload=payload_data + 12),
            linktype=cap.linktype, big_endian=cap.big_endian))
        urb_id += 1

    slot_len = payload_data + 16

    def emit_frame(k: int, fid: int, ts0: int, *, err_at: Optional[int] = None,
                   truncate: bool = False, partial_from: int = 0) -> Optional[int]:
        """Emit frame k's payloads as ISO URBs; returns the ts of its first URB."""
        nonlocal urb_id
        data = ramp_frame_y16(k, width, height)
        chunks = [data[i:i + payload_data] for i in range(0, len(data), payload_data)]
        payloads = [uvc_payload(c, fid=fid, eof=(i == len(chunks) - 1),
                                err=(err_at is not None and i == err_at), hlen=hlen)
                    for i, c in enumerate(chunks)]
        payloads = payloads[partial_from:]
        first_ts = None
        for j in range(0, len(payloads), pkts_per_urb):
            group = payloads[j:j + pkts_per_urb]
            ts = cap.quant(ts0 + (j // pkts_per_urb) * 1_000_000)
            first_ts = first_ts if first_ts is not None else ts
            cap.add(max(0, ts - 500_000), usbmon_record(
                urb_id=urb_id, rtype="S", xfer=_ISO, ep=0x80 | ep, dev=dev, bus=bus,
                flag_data=ord("<"), length=slot_len * len(group),
                linktype=cap.linktype, big_endian=cap.big_endian))
            trunc = None
            if truncate and j == 0 and len(group) > 1:
                # cut the RECORD inside payload 2's slot; len_cap still claims everything
                hdr_len = 64 if cap.linktype == 220 else 48
                trunc = hdr_len + 16 * len(group) + slot_len + len(group[1]) // 2
            cap.add(ts, iso_urb(group, urb_id=urb_id, ep=ep, dev=dev, bus=bus,
                                slot_len=slot_len, linktype=cap.linktype,
                                big_endian=cap.big_endian, truncate_at=trunc))
            urb_id += 1
        return first_ts

    fid = 0
    if leading_partial:
        # the capture starts mid-frame: the tail of a frame that must be discarded
        emit_frame(-1, fid, base_ns - frame_period_ns, partial_from=2)
        fid ^= 1
    for k in range(frames):
        ts0 = base_ns + k * frame_period_ns
        if k == omit_frame:
            fid ^= 1   # the frame existed on the wire but usbmon lost every URB of it:
            continue   # the NEXT frame repeats the pre-loss FID
        if k == err_frame:
            emit_frame(k, fid, ts0, err_at=1)
        elif k == truncated_frame:
            emit_frame(k, fid, ts0, truncate=True)
        else:
            first_ts = emit_frame(k, fid, ts0)
            expected.append((first_ts, ramp_frame_y16(k, width, height)))
        if k % 2 == 0:
            _noise(cap, cap.quant(ts0 + 3_000_000), dev + 3, urb_id)
            urb_id += 2
        fid ^= 1
    return cap.getvalue(), expected


def build_mjpeg_capture(*, frames: int = 5, fmt: str = "pcap", bus: int = 1, dev: int = 6,
                        ep: int = 2, base_ns: int = 1_700_000_000_000_000_000,
                        frame_period_ns: int = 33_333_000, payload_data: int = 128,
                        ) -> Tuple[bytes, List[Tuple[int, bytes]]]:
    """ISO MJPEG capture: variable-size SOI..EOI blobs, one with trailing 0x00 padding."""
    cap = _Capture(fmt)
    expected: List[Tuple[int, bytes]] = []
    urb_id = 0xFFFF990000000000
    fid = 0
    slot_len = payload_data + 16
    for k in range(frames):
        jpeg = make_jpeg(k, extra=57 * k)   # valid + byte-distinct + size-varying
        wire = jpeg + (b"\x00" * 10 if k == 2 else b"")   # padded frame: parser must strip
        chunks = [wire[i:i + payload_data] for i in range(0, len(wire), payload_data)]
        payloads = [uvc_payload(c, fid=fid, eof=(i == len(chunks) - 1))
                    for i, c in enumerate(chunks)]
        first_ts = None
        for j in range(0, len(payloads), 4):
            ts = cap.quant(base_ns + k * frame_period_ns + (j // 4) * 1_000_000)
            first_ts = first_ts if first_ts is not None else ts
            cap.add(ts, iso_urb(payloads[j:j + 4], urb_id=urb_id, ep=ep, dev=dev, bus=bus,
                                slot_len=slot_len, linktype=cap.linktype,
                                big_endian=cap.big_endian))
            urb_id += 1
        expected.append((first_ts, jpeg))
        fid ^= 1
    return cap.getvalue(), expected


def build_bulk_capture(*, frames: int = 4, width: int = 32, height: int = 4,
                       fmt: str = "pcap", bus: int = 1, dev: int = 7, ep: int = 3,
                       base_ns: int = 1_700_000_000_000_000_000,
                       frame_period_ns: int = 16_666_000, urb_size: int = 256,
                       zlp: bool = False,
                       ) -> Tuple[bytes, List[Tuple[int, bytes]]]:
    """Bulk capture: one UVC payload per frame, SPANNING URBs. A payload whose length is
    NOT a multiple of urb_size ends on a short URB; with zlp=True, sizes must be an exact
    multiple and each payload is terminated by a zero-length completion instead."""
    cap = _Capture(fmt)
    expected: List[Tuple[int, bytes]] = []
    urb_id = 0xFFFFAA0000000000
    fid = 0
    for k in range(frames):
        data = ramp_frame_y16(k, width, height)
        wire = uvc_payload(data, fid=fid, eof=True)
        assert (len(wire) % urb_size == 0) == zlp, \
            "sizes must end on a SHORT urb, or be an exact multiple with zlp=True"
        pieces = [wire[i:i + urb_size] for i in range(0, len(wire), urb_size)]
        first_ts = None
        for j, piece in enumerate(pieces):
            ts = cap.quant(base_ns + k * frame_period_ns + j * 500_000)
            first_ts = first_ts if first_ts is not None else ts
            cap.add(ts, usbmon_record(
                urb_id=urb_id, rtype="S", xfer=_BULK, ep=0x80 | ep, dev=dev, bus=bus,
                flag_data=ord("<"), length=urb_size,
                linktype=cap.linktype, big_endian=cap.big_endian))
            cap.add(ts, usbmon_record(
                urb_id=urb_id, rtype="C", xfer=_BULK, ep=0x80 | ep, dev=dev, bus=bus,
                length=len(piece), data=piece,
                linktype=cap.linktype, big_endian=cap.big_endian))
            urb_id += 1
        if zlp:
            ts = cap.quant(base_ns + k * frame_period_ns + len(pieces) * 500_000)
            cap.add(ts, usbmon_record(
                urb_id=urb_id, rtype="C", xfer=_BULK, ep=0x80 | ep, dev=dev, bus=bus,
                length=0, flag_data=ord("<"),
                linktype=cap.linktype, big_endian=cap.big_endian))
            urb_id += 1
        expected.append((first_ts, data))
        fid ^= 1
    return cap.getvalue(), expected
