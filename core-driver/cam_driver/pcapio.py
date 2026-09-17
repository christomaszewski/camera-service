"""Capture-file readers: classic pcap + pcapng -> (link_type, ts_ns, record_bytes).

Pure stdlib (struct/io), streaming -- one record in RAM at a time, so a multi-GB usbmon
capture never loads. Knows nothing about USB: the usbmon/UVC layer (uvcpcap) filters by
link type and parses record bytes.

Format notes that matter here:
  * classic pcap's magic encodes BOTH endianness and timestamp resolution (us vs ns).
  * pcapng (what Wireshark saves by default) is block-structured; endianness is per
    SECTION (SHB byte-order magic) and timestamp resolution is per INTERFACE
    (if_tsresol option, default 10^-6 s). An EPB's timestamp is meaningless without
    its interface's tsresol, so interfaces are tracked per section.
  * A capture killed mid-write (Ctrl-C'd Wireshark/tcpdump) legitimately ends in a
    truncated record/block: stop cleanly there, keeping everything before it.
"""
from __future__ import annotations

import logging
import struct
from typing import BinaryIO, Iterator, Tuple

log = logging.getLogger(__name__)

# A corrupt length field must not make us attempt a giant read; no real usbmon record
# (64B header + <=128 iso descriptors + one URB buffer) comes anywhere near this.
_MAX_RECORD = 256 * 1024 * 1024

# classic pcap magics, as the raw first-4-bytes on disk
_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1000),   # LE writer, ts in microseconds
    b"\xa1\xb2\xc3\xd4": (">", 1000),   # BE writer, microseconds
    b"\x4d\x3c\xb2\xa1": ("<", 1),      # LE writer, nanoseconds
    b"\xa1\xb2\x3c\x4d": (">", 1),      # BE writer, nanoseconds
}
_PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"     # SHB block type (byte-symmetric)

_SHB = 0x0A0D0D0A
_IDB = 0x00000001
_SPB = 0x00000003
_EPB = 0x00000006


class PcapFormatError(ValueError):
    """File-format problem; message names the file, offset, and found-vs-expected."""


def sniff_format(path: str) -> str:
    """'pcap' | 'pcap-ns' | 'pcapng' (with '-be' suffix for a big-endian writer)."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic == _PCAPNG_MAGIC:
        return "pcapng"
    if magic in _PCAP_MAGICS:
        endian, frac = _PCAP_MAGICS[magic]
        name = "pcap" if frac == 1000 else "pcap-ns"
        return name + ("-be" if endian == ">" else "")
    raise PcapFormatError(
        f"{path}: not a pcap or pcapng file (first bytes {magic.hex() or '<empty>'})")


def iter_packets(path: str) -> Iterator[Tuple[int, int, bytes]]:
    """Yield (link_type, ts_ns, record_bytes) in file order, streaming.

    Tolerates a truncated final record/block (stops with a debug log). Raises
    PcapFormatError on a bad magic or a corrupt block with an actionable message.
    """
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic == _PCAPNG_MAGIC:
            yield from _iter_pcapng(f, path)
        elif magic in _PCAP_MAGICS:
            endian, frac = _PCAP_MAGICS[magic]
            yield from _iter_pcap(f, path, endian, frac)
        else:
            raise PcapFormatError(
                f"{path}: not a pcap or pcapng file (first bytes {magic.hex() or '<empty>'})")


# ---- classic pcap ----------------------------------------------------------

def _iter_pcap(f: BinaryIO, path: str, endian: str, frac_ns: int) -> Iterator[Tuple[int, int, bytes]]:
    ghdr = f.read(20)   # after the 4 magic bytes: version(4) thiszone(4) sigfigs(4) snaplen(4) linktype(4)
    if len(ghdr) < 20:
        raise PcapFormatError(f"{path}: truncated pcap global header")
    _vmaj, _vmin, _zone, _sig, snaplen, linktype = struct.unpack(endian + "HHiIII", ghdr)
    if snaplen and snaplen < 65535:
        log.warning("%s: snaplen=%d -- records larger than this were cut at capture "
                    "time and their frames will be dropped", path, snaplen)
    rec_hdr = struct.Struct(endian + "IIII")
    while True:
        hdr = f.read(16)
        if len(hdr) < 16:
            if hdr:
                log.debug("%s: truncated record header at offset %d; stopping", path, f.tell() - len(hdr))
            return
        ts_sec, ts_frac, incl_len, _orig_len = rec_hdr.unpack(hdr)
        if incl_len > _MAX_RECORD:
            raise PcapFormatError(
                f"{path}: record at offset {f.tell() - 16} claims {incl_len} bytes -- corrupt file")
        data = f.read(incl_len)
        if len(data) < incl_len:
            log.debug("%s: truncated final record (%d of %d bytes); stopping", path, len(data), incl_len)
            return
        yield linktype, ts_sec * 1_000_000_000 + ts_frac * frac_ns, data


# ---- pcapng ----------------------------------------------------------------

def _tsresol_to_ns(tsresol: int):
    """Converter raw-ts -> ns for an if_tsresol value (MSB set = 2^-v, else 10^-v)."""
    if tsresol & 0x80:
        v = tsresol & 0x7F
        return lambda ts: (ts * 1_000_000_000) >> v
    if tsresol <= 9:
        mult = 10 ** (9 - tsresol)
        return lambda ts: ts * mult
    div = 10 ** (tsresol - 9)
    return lambda ts: ts // div


def _iter_pcapng(f: BinaryIO, path: str) -> Iterator[Tuple[int, int, bytes]]:
    endian = None
    interfaces = []   # per section: list of (link_type, ts_to_ns)
    spb_warned = False
    first = _PCAPNG_MAGIC   # the 4 bytes already consumed by the dispatcher
    while True:
        btype_raw = first if first is not None else f.read(4)
        first = None
        if len(btype_raw) < 4:
            return
        if btype_raw == _PCAPNG_MAGIC:
            # SHB: endianness comes from the byte-order magic INSIDE the body, so read
            # total_length + byte_order_magic raw, resolve endianness, then re-decode.
            head = f.read(8)
            if len(head) < 8:
                log.debug("%s: truncated SHB; stopping", path)
                return
            bom = head[4:8]   # 0x1A2B3C4D as the writer stored it: LE writer -> 4d 3c 2b 1a
            if bom == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif bom == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise PcapFormatError(f"{path}: SHB with bad byte-order magic {bom.hex()}")
            (total_len,) = struct.unpack(endian + "I", head[0:4])
            if not 12 <= total_len <= _MAX_RECORD:
                raise PcapFormatError(f"{path}: SHB with absurd length {total_len}")
            if len(f.read(total_len - 12)) < total_len - 12:   # rest of the SHB body
                log.debug("%s: truncated SHB body; stopping", path)
                return
            interfaces = []   # a new section resets the interface list
            continue
        if endian is None:
            raise PcapFormatError(f"{path}: pcapng file does not start with a Section Header Block")
        (btype,) = struct.unpack(endian + "I", btype_raw)
        lenb = f.read(4)
        if len(lenb) < 4:
            return
        (total_len,) = struct.unpack(endian + "I", lenb)
        if not 12 <= total_len <= _MAX_RECORD or total_len % 4:
            raise PcapFormatError(
                f"{path}: block type 0x{btype:08x} at offset {f.tell() - 8} with bad length {total_len}")
        body = f.read(total_len - 12)
        trailer = f.read(4)
        if len(body) < total_len - 12 or len(trailer) < 4:
            log.debug("%s: truncated final block (type 0x%08x); stopping", path, btype)
            return

        if btype == _IDB:
            if len(body) < 8:
                raise PcapFormatError(f"{path}: IDB shorter than its fixed fields")
            link_type, _resv, _snap = struct.unpack(endian + "HHI", body[0:8])
            tsresol = 6   # default: microseconds
            opts = body[8:]
            while len(opts) >= 4:
                code, olen = struct.unpack(endian + "HH", opts[0:4])
                if code == 0:     # opt_endofopt
                    break
                if code == 9 and olen >= 1:   # if_tsresol
                    tsresol = opts[4]
                opts = opts[4 + (olen + 3) // 4 * 4:]
            interfaces.append((link_type, _tsresol_to_ns(tsresol)))
        elif btype == _EPB:
            if len(body) < 20:
                raise PcapFormatError(f"{path}: EPB shorter than its fixed fields")
            if_id, ts_hi, ts_lo, cap_len, _orig = struct.unpack(endian + "IIIII", body[0:20])
            if if_id >= len(interfaces):
                raise PcapFormatError(
                    f"{path}: EPB references interface {if_id} but only {len(interfaces)} declared")
            if cap_len > len(body) - 20:
                raise PcapFormatError(
                    f"{path}: EPB claims {cap_len} captured bytes but the block holds {len(body) - 20}")
            link_type, to_ns = interfaces[if_id]
            yield link_type, to_ns((ts_hi << 32) | ts_lo), body[20:20 + cap_len]
        elif btype == _SPB:
            if not spb_warned:
                spb_warned = True
                log.warning("%s: Simple Packet Blocks carry no timestamp; skipping them "
                            "(frames inside are unusable for replay)", path)
        # NRB/ISB/custom/unknown blocks: skipped by length
