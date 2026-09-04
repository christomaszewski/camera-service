"""Tests for the pcap/pcapng readers (pure; no GStreamer).

Run: python3 core-driver/tests/test_pcapio.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.pcapio import PcapFormatError, iter_packets, sniff_format  # noqa: E402
from uvcpcap_fixture import PcapWriter, PcapngWriter  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="pcapio_test_")
BASE = 1_700_000_000_000_000_000


def _path(data: bytes, name: str) -> str:
    p = os.path.join(_TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_pcap_us_and_ns_magics():
    for ns, name in ((False, "us.pcap"), (True, "ns.pcap")):
        w = PcapWriter(linktype=220, nanosecond=ns)
        ts = BASE + (123_456_789 if ns else 123_456_000)   # us files hold whole microseconds
        w.add(ts, b"hello")
        got = list(iter_packets(_path(w.getvalue(), name)))
        assert got == [(220, ts, b"hello")]
    assert sniff_format(_path(PcapWriter().getvalue(), "s.pcap")) == "pcap"
    assert sniff_format(_path(PcapWriter(nanosecond=True).getvalue(), "sn.pcap")) == "pcap-ns"


def test_pcap_byte_swapped():
    w = PcapWriter(linktype=220, big_endian=True)
    w.add(BASE + 5_000, b"\x01\x02")
    got = list(iter_packets(_path(w.getvalue(), "be.pcap")))
    assert got == [(220, BASE + 5_000, b"\x01\x02")]
    assert sniff_format(_path(w.getvalue(), "be2.pcap")) == "pcap-be"


def test_pcap_truncated_tail_stops_cleanly():
    w = PcapWriter()
    w.add(BASE, b"full record")
    blob = w.getvalue() + struct.pack("<IIII", 1, 2, 100, 100) + b"only-ten-b"
    got = list(iter_packets(_path(blob, "trunc.pcap")))
    assert got == [(220, BASE, b"full record")]


def test_pcap_bad_magic():
    p = _path(b"\x00\x01\x02\x03garbage", "bad.pcap")
    try:
        list(iter_packets(p))
        raise AssertionError("expected PcapFormatError")
    except PcapFormatError as e:
        assert "not a pcap" in str(e)


def test_pcapng_tsresol_variants():
    # decimal 6 (default granularity), decimal 9, binary 2^-9 (1e9 = 2^9 * 1953125)
    for tsresol, step in ((6, 1_000), (9, 1), (0x80 | 9, 1_953_125), (None, 1_000)):
        w = PcapngWriter()
        ifid = w.add_idb(220, tsresol=tsresol)
        eff = 6 if tsresol is None else tsresol
        ts = BASE + 42 * step
        w.add_epb(ifid, ts, b"payload", tsresol=eff)
        got = list(iter_packets(_path(w.getvalue(), f"resol{tsresol}.pcapng")))
        assert got == [(220, ts, b"payload")], f"tsresol={tsresol}"


def test_pcapng_multi_interface_linktypes():
    w = PcapngWriter()
    usb = w.add_idb(220)
    eth = w.add_idb(1)
    w.add_epb(usb, BASE, b"usb-record")
    w.add_epb(eth, BASE + 1, b"eth-record")
    got = list(iter_packets(_path(w.getvalue(), "multi.pcapng")))
    assert got == [(220, BASE, b"usb-record"), (1, BASE + 1, b"eth-record")]


def test_pcapng_two_sections_reset_interfaces():
    w = PcapngWriter()
    a = w.add_idb(220)
    w.add_epb(a, BASE, b"one")
    w.add_shb()                      # new section: interface list resets
    b = w.add_idb(1)                 # interface 0 of the NEW section is Ethernet
    w.add_epb(b, BASE + 1, b"two")
    got = list(iter_packets(_path(w.getvalue(), "sections.pcapng")))
    assert got == [(220, BASE, b"one"), (1, BASE + 1, b"two")]


def test_pcapng_unknown_and_spb_blocks_skipped():
    w = PcapngWriter()
    ifid = w.add_idb(220)
    w._block(0x00000004, b"\x00" * 20)            # NRB: skipped
    w._block(0x00000003, struct.pack("<I", 4) + b"data")   # SPB: skipped (no timestamp)
    w.add_epb(ifid, BASE, b"kept")
    got = list(iter_packets(_path(w.getvalue(), "blocks.pcapng")))
    assert got == [(220, BASE, b"kept")]


def test_pcapng_truncated_final_block():
    w = PcapngWriter()
    ifid = w.add_idb(220)
    w.add_epb(ifid, BASE, b"kept")
    blob = w.getvalue() + struct.pack("<II", 6, 64) + b"short"
    got = list(iter_packets(_path(blob, "truncblk.pcapng")))
    assert got == [(220, BASE, b"kept")]


def test_pcapng_bad_interface_id():
    w = PcapngWriter()
    w.add_idb(220)
    w.add_epb(0, BASE, b"x")
    blob = w.getvalue()
    w2 = PcapngWriter()
    w2.add_idb(220)
    body = struct.pack("<IIIII", 7, 0, 0, 1, 1) + b"x\x00\x00\x00"   # interface 7 undeclared
    w2._block(0x00000006, body)
    try:
        list(iter_packets(_path(w2.getvalue(), "badif.pcapng")))
        raise AssertionError("expected PcapFormatError")
    except PcapFormatError as e:
        assert "interface" in str(e)
    assert list(iter_packets(_path(blob, "goodif.pcapng"))) == [(220, BASE, b"x")]


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
