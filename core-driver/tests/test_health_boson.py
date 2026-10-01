"""Tests for the FLIR Boson health provider (cam_driver.health_boson): the serial framing (escaping,
CRC-16/AUG-CCITT, frame splitting), and the provider end to end against a SIMULATED Boson answering
the command protocol on a pty -- the same byte stream a real camera's CDC-ACM port carries. Plus the
config keys (usb.control_protocol / control_device) and the from_config wiring.

Pure stdlib (os.openpty) -- no camera, no gi, no pyserial -- so this runs (and must not skip) in CI.

Run: python3 core-driver/tests/test_health_boson.py
"""
import os
import select
import struct
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.config import parse_config  # noqa: E402
from cam_driver.health import OK, STALE, HealthMonitor  # noqa: E402
from cam_driver.health_boson import (FN_CAMERA_SN, FN_FFC_STATE, FN_FPA_TEMP, FN_FRAME_COUNT,  # noqa: E402
                                     FN_LAST_FFC_FRAME, FN_PART_NUMBER, BosonError, BosonPort,
                                     BosonProvider, FrameSplitter, crc16, decode_frame, encode_frame,
                                     escape, unescape)


# ---- framing ------------------------------------------------------------------------
def test_crc_is_aug_ccitt():
    assert crc16(b"123456789") == 0xE5CC, "CRC-16/AUG-CCITT check value (poly 0x1021, init 0x1D0F)"


def test_escaping_round_trips_every_marker_byte():
    body = bytes([0x00, 0x8E, 0x9E, 0xAE, 0x81, 0x91, 0xA1, 0xFF])
    esc = escape(body)
    assert 0x8E not in esc and 0xAE not in esc, "no frame marker survives inside a body"
    assert esc == bytes([0x00, 0x9E, 0x81, 0x9E, 0x91, 0x9E, 0xA1, 0x81, 0x91, 0xA1, 0xFF])
    assert unescape(esc) == body


def test_a_request_frame_matches_the_documented_layout():
    f = encode_frame(1, FN_FPA_TEMP)
    assert f[0] == 0x8E and f[-1] == 0xAE
    body = unescape(f[1:-1])
    assert body[:13] == bytes([0]) + struct.pack(">III", 1, 0x00050030, 0xFFFFFFFF), \
        "channel 0 | seq | function id | status 0xFFFFFFFF"
    assert struct.unpack(">H", body[13:])[0] == crc16(body[:13])
    assert decode_frame(f) == (1, FN_FPA_TEMP, 0xFFFFFFFF, b"")


def test_decode_refuses_corruption():
    f = bytearray(encode_frame(7, FN_FFC_STATE, 0, b"\x00\x03"))
    f[5] ^= 0x01
    for bad, needle in ((bytes(f), "CRC"), (b"\x8e\x00\xae", "short"), (b"\x00\x01", "not a frame")):
        try:
            decode_frame(bad)
        except BosonError as e:
            assert needle in str(e), (needle, e)
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_the_splitter_finds_frames_across_reads_and_drops_garbage():
    a, b = encode_frame(1, FN_FPA_TEMP, 0, b"\x01\x9a"), encode_frame(2, FN_FFC_STATE, 0, b"\x00\x8e")
    stream = b"\x01\x02junk" + a + b
    sp = FrameSplitter()
    got = []
    for i in range(0, len(stream), 3):
        got += sp.feed(stream[i:i + 3])
    assert got == [a, b]


# ---- a simulated Boson on a pty -----------------------------------------------------------
class FakeBoson:
    """Answers the command protocol on the master side of a pty; the provider opens the slave path,
    exactly as it opens /dev/serial/by-id/... on a vehicle."""

    def __init__(self):
        self.master, slave = os.openpty()
        self.path = os.ttyname(slave)
        self._slave = slave            # keep the slave open so the pty survives provider reopen()s
        self.fpa_dc = 412              # 41.2 C
        self.ffc = 3
        self.frames = 5000
        self.last_ffc = 4200
        self.silent = False            # stop answering entirely
        self.fail = {}                 # function id -> return code
        self.requests = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _reply_payload(self, fid):
        return {FN_FPA_TEMP: struct.pack(">h", self.fpa_dc), FN_FFC_STATE: struct.pack(">H", self.ffc),
                FN_FRAME_COUNT: struct.pack(">I", self.frames), FN_LAST_FFC_FRAME: struct.pack(">I", self.last_ffc),
                FN_CAMERA_SN: struct.pack(">I", 123456),
                FN_PART_NUMBER: b"20640000A".ljust(20, b"\0")}.get(fid)

    def _run(self):
        sp = FrameSplitter()
        while not self._stop.is_set():
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            for f in sp.feed(data):
                seq, fid, status, _ = decode_frame(f)
                assert status == 0xFFFFFFFF, "requests carry status 0xFFFFFFFF"
                self.requests.append(fid)
                if self.silent:
                    continue
                code = self.fail.get(fid, 0)
                payload = b"" if code else (self._reply_payload(fid) or b"")
                if not code and payload == b"":
                    code = 0x0161
                os.write(self.master, encode_frame(seq, fid, code, payload))

    def close(self):
        self._stop.set()
        self._t.join(1)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


def _provider(cam, **kw):
    return BosonProvider(cam.path, port_factory=lambda p: BosonPort(p, timeout_s=0.3), **kw)


def test_the_provider_reads_the_camera_over_its_command_channel():
    cam = FakeBoson()
    try:
        prov = _provider(cam)
        [r] = prov.poll()
        assert r.component == "camera" and r.level == OK and r.message == "41.2 C"
        assert r.hardware_id == "FLIR Boson 20640000A sn 123456", "read once, at open"
        assert r.values == {"temp.sensor_c": 41.2, "ffc.state": "complete", "frame_count": 5000,
                            "ffc.last_frame": 4200, "ffc.frames_since": 800}
        n = len(cam.requests)
        prov.poll()
        assert cam.requests[n:] == [FN_FPA_TEMP, FN_FFC_STATE, FN_FRAME_COUNT, FN_LAST_FFC_FRAME], \
            "the identity is not re-read every poll"
        prov.close()
    finally:
        cam.close()


def test_negative_temperatures_and_an_ffc_in_progress():
    cam = FakeBoson()
    try:
        cam.fpa_dc, cam.ffc = -53, 2
        [r] = _provider(cam).poll()
        assert r.values["temp.sensor_c"] == -5.3, "the FPA temperature is SIGNED"
        assert r.level == OK and r.message == "-5.3 C; FFC in progress", \
            "an FFC is normal operation: said, not alarmed"
    finally:
        cam.close()


def test_one_refused_command_skips_its_value_only():
    cam = FakeBoson()
    try:
        cam.fail = {FN_LAST_FFC_FRAME: 0x0161}
        [r] = _provider(cam).poll()
        assert r.values["temp.sensor_c"] == 41.2 and "ffc.last_frame" not in r.values
        assert "ffc.frames_since" not in r.values and r.values["read_errors"] == 1
    finally:
        cam.close()


def test_a_silent_camera_raises_so_the_monitor_can_go_stale():
    cam = FakeBoson()
    try:
        cam.silent = True
        prov = _provider(cam)
        try:
            prov.poll()
        except BosonError as e:
            assert "camera not answering" in str(e)
        else:
            raise AssertionError("a camera that answers nothing must raise")
        assert prov._port is None, "closed, to be reopened on a later poll"
        cam.silent = False
        [r] = prov.poll()
        assert r.values["temp.sensor_c"] == 41.2, "reopened and reading again"
    finally:
        cam.close()


def test_a_missing_device_goes_stale_through_the_monitor():
    clock = [0.0]
    prov = BosonProvider("/dev/serial/by-id/usb-FLIR_Boson_nope-if00")
    m = HealthMonitor([prov], instance="c", clock=lambda: clock[0], stale_after_s=5.0)
    [st] = m.poll_once()["status"]
    assert st["level"] == STALE and st["name"] == "c: camera" and "No such file" in st["message"]


def test_a_late_reply_is_never_taken_for_the_next_command():
    cam = FakeBoson()
    try:
        port = BosonPort(cam.path, timeout_s=0.3)
        port.open()
        os.write(cam.master, encode_frame(999, FN_FPA_TEMP, 0, struct.pack(">h", 999)))   # stale junk
        assert struct.unpack(">h", port.command(FN_FPA_TEMP))[0] == 412
        port.close()
    finally:
        cam.close()


# ---- config + wiring ------------------------------------------------------------------
_USB = {"device": "/dev/v4l/by-id/usb-FLIR_Boson_1-video-index0", "pixel_format": "GRAY16_LE",
        "control_protocol": "FLIR-Boson", "control_device": " /dev/serial/by-id/usb-FLIR_Boson_1-if00 "}


def test_usb_control_keys_parse_and_validate():
    u = parse_config({"camera": {"type": "usb"}, "usb": _USB}).usb
    assert u.control_protocol == "flir-boson" and u.control_device == "/dev/serial/by-id/usb-FLIR_Boson_1-if00"
    assert parse_config({}).usb.control_protocol == "" and parse_config({}).usb.control_device == ""
    for usb, needle in (({"control_protocol": "lepton", "control_device": "/dev/x"}, "usb.control_protocol: expected"),
                        ({"control_protocol": "flir-boson"}, "usb.control_device: required"),
                        ({"control_device": "/dev/ttyACM0"}, "usb.control_protocol: required")):
        try:
            parse_config({"usb": usb})
        except ValueError as e:
            assert needle in str(e), (usb, e)
        else:
            raise AssertionError(f"accepted {usb!r}")


class _Src:
    playback = None
    active_timestamp_source = "sof"

    def genicam(self):
        return None


class _Pipe:
    source = _Src()
    frame_rate = 60.0
    reconnecting = False


class _Lc:
    def descriptor(self):
        return {"state": "inactive", "recording_enabled": False, "health": {}}


def _names(raw):
    return [p.name for p in HealthMonitor.from_config(parse_config(raw), instance="c", lifecycle=_Lc(),
                                                      pipeline=_Pipe()).providers]


def test_from_config_adds_the_boson_provider_from_the_source_keys():
    assert _names({"camera": {"type": "usb"}, "usb": _USB}) == ["pipeline", "boson"]
    assert _names({"camera": {"type": "usb"}, "usb": dict(_USB, fake=True)}) == ["pipeline"], "fake: no device"
    assert _names({"camera": {"type": "usb"}, "usb": _USB,
                   "health": {"providers": {"boson": {"enabled": False}}}}) == ["pipeline"]
    assert _names({"camera": {"type": "usb"}, "usb": {"device": "/dev/video0"},
                   "health": {"providers": {"boson": {"enabled": True}}}}) == ["pipeline"], \
        "enabled without a channel: warned + skipped"
    m = HealthMonitor.from_config(parse_config({"camera": {"type": "usb"}, "usb": _USB}), instance="c",
                                  lifecycle=_Lc(), pipeline=_Pipe())
    assert m.providers[1].device == "/dev/serial/by-id/usb-FLIR_Boson_1-if00"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")
