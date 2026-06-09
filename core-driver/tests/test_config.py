"""Tests for config parsing (pure dict -> AppConfig; no YAML/GStreamer needed).

Run: python3 core-driver/tests/test_config.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.config import parse_config  # noqa: E402
from cam_driver.sources import make_source  # noqa: E402


def test_transport_defaults():
    c = parse_config({})
    assert c.transport.plugin_endpoint.enabled is True
    assert c.transport.plugin_endpoint.socket_path == "/tmp/cam/frames"
    assert c.transport.plugin_endpoint.max_rate_hz == 0.0
    assert c.transport.raw_endpoint.enabled is False
    assert c.plugins == []


def test_partial_endpoint_overlay_keeps_defaults():
    # supplying only max_rate_hz must not clobber the default enabled/socket_path
    c = parse_config({"transport": {"plugin_endpoint": {"max_rate_hz": 10}}})
    assert c.transport.plugin_endpoint.max_rate_hz == 10
    assert c.transport.plugin_endpoint.enabled is True
    assert c.transport.plugin_endpoint.socket_path == "/tmp/cam/frames"


def test_raw_endpoint_enable():
    c = parse_config({"transport": {"raw_endpoint": {"enabled": True, "socket_path": "/tmp/x"}}})
    assert c.transport.raw_endpoint.enabled is True
    assert c.transport.raw_endpoint.socket_path == "/tmp/x"


def test_plugins_parsed_with_params():
    c = parse_config({"plugins": [
        {"name": "ros2-bridge", "enabled": True, "topic": "/cam/image", "frame_id": "camera"},
        {"name": "mqtt", "enabled": False},
        {"no_name": "skipped"},
    ]})
    assert [p.name for p in c.plugins] == ["ros2-bridge", "mqtt"]   # nameless entry dropped
    assert c.plugins[0].enabled is True
    assert c.plugins[0].params == {"topic": "/cam/image", "frame_id": "camera"}
    assert c.plugins[1].enabled is False


def test_plugin_command_and_restart():
    c = parse_config({"plugins": [
        {"name": "probe", "command": ["python3", "x.py"], "restart": False, "extra": 1},
    ]})
    p = c.plugins[0]
    assert p.command == ["python3", "x.py"]
    assert p.restart is False
    assert p.params == {"extra": 1}   # command/restart are not left in params


# ---- camera (general block) + source `type` ----------------------------------
def test_camera_defaults_to_gige():
    c = parse_config({})
    assert c.camera.type == "gige"
    assert c.camera.frame_rate is None
    assert c.camera.reconnect is True and c.camera.reconnect_timeout_s == 5.0
    assert (c.camera.reconnect_backoff_s, c.camera.reconnect_backoff_max_s) == (1.0, 30.0)


def test_camera_type_parsed():
    assert parse_config({"camera": {"type": "usb"}}).camera.type == "usb"
    assert parse_config({"camera": {"type": "rtsp"}}).camera.type == "rtsp"


def test_make_source_unknown_raises():
    # factory dispatch is testable without a source's native deps (impls import lazily)
    try:
        make_source(parse_config({"camera": {"type": "nope"}}))
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---- gige block --------------------------------------------------------------
def test_gige_defaults():
    g = parse_config({}).gige
    assert g.pixel_format == "Mono8" and g.camera_id is None
    assert g.timestamp_source == "ptp_chunk" and g.ptp_enable is True


def test_gige_block_parsed_with_roi():
    c = parse_config({"camera": {"type": "gige"},
                      "gige": {"camera_id": "Lucid-1", "pixel_format": "Mono16", "ptp_enable": False,
                               "roi": {"x": 4, "y": 8, "width": 64, "height": 32}}})
    assert c.gige.camera_id == "Lucid-1" and c.gige.pixel_format == "Mono16"
    assert c.gige.ptp_enable is False
    assert (c.gige.roi.x, c.gige.roi.width, c.gige.roi.height) == (4, 64, 32)


def test_unknown_keys_ignored():
    c = parse_config({"gige": {"pixel_format": "Mono12", "totally_unknown": 5}})
    assert c.gige.pixel_format == "Mono12"


# ---- usb block ---------------------------------------------------------------
def test_usb_config_defaults():
    u = parse_config({}).usb
    assert (u.device, u.fake, u.pixel_format) == ("/dev/video0", False, "GRAY8")
    assert u.sof_timestamps is False        # SOF (v4l2 driver ts) is opt-in; default = arrival


def test_usb_block_parsed():
    c = parse_config({"camera": {"type": "usb"},
                      "usb": {"fake": True, "width": 640, "height": 480, "device": "/dev/video2",
                              "sof_timestamps": True}})
    assert c.camera.type == "usb"
    assert c.usb.fake is True and (c.usb.width, c.usb.height) == (640, 480)
    assert c.usb.device == "/dev/video2" and c.usb.sof_timestamps is True


# ---- rtsp block --------------------------------------------------------------
def test_rtsp_config_defaults():
    r = parse_config({}).rtsp
    assert (r.codec, r.latency_ms) == ("h264", 200) and r.url.startswith("rtsp://")
    assert r.probe is True        # self-configure from the live stream by default


def test_rtsp_block_parsed():
    c = parse_config({"camera": {"type": "rtsp"},
                      "rtsp": {"url": "rtsp://x/y", "codec": "mjpeg", "probe": False, "protocols": "tcp"}})
    assert c.camera.type == "rtsp"
    assert (c.rtsp.url, c.rtsp.codec, c.rtsp.protocols) == ("rtsp://x/y", "mjpeg", "tcp")
    assert c.rtsp.probe is False


# ---- general -> source overlay (the symmetric schema's key behaviour) --------
def test_frame_rate_overlay():
    # camera.frame_rate (general) overlays every source's effective config...
    c = parse_config({"camera": {"frame_rate": 20.0}})
    assert c.gige.frame_rate == 20.0 and c.usb.frame_rate == 20.0 and c.rtsp.frame_rate == 20.0
    # ...and when unset, each source keeps its own sensible default.
    d = parse_config({})
    assert d.gige.frame_rate is None and d.usb.frame_rate == 30.0 and d.rtsp.frame_rate == 30.0


def test_reconnect_overlay():
    # reconnect is GENERAL -> set once under camera:, overlaid onto the active source's config.
    c = parse_config({"camera": {"type": "usb", "reconnect": False, "reconnect_timeout_s": 7.0}})
    assert c.usb.reconnect is False and c.usb.reconnect_timeout_s == 7.0
    assert c.gige.reconnect is False and c.rtsp.reconnect is False   # overlaid onto all source blocks
    # default: on, 5s, on every source.
    d = parse_config({})
    assert d.gige.reconnect is True and d.gige.reconnect_timeout_s == 5.0
    assert d.usb.reconnect is True and d.rtsp.reconnect_timeout_s == 5.0


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
