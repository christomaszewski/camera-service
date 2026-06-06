"""Tests for config parsing (pure dict -> AppConfig; no YAML/GStreamer needed).

Run: python3 core-driver/tests/test_config.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.config import parse_config  # noqa: E402
from cam_driver.sources import make_source  # noqa: E402


def test_defaults():
    c = parse_config({})
    assert c.transport.plugin_endpoint.enabled is True
    assert c.transport.plugin_endpoint.socket_path == "/tmp/cam/frames"
    assert c.transport.plugin_endpoint.max_rate_hz == 0.0
    assert c.transport.raw_endpoint.enabled is False
    assert c.plugins == []
    assert c.camera.pixel_format == "Mono8"


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


def test_unknown_keys_ignored():
    c = parse_config({"camera": {"pixel_format": "Mono12", "totally_unknown": 5}})
    assert c.camera.pixel_format == "Mono12"


def test_fake_and_roi():
    c = parse_config({"camera": {"fake": True, "roi": {"x": 4, "y": 8, "width": 64, "height": 32}}})
    assert c.camera.fake is True
    assert (c.camera.roi.x, c.camera.roi.width, c.camera.roi.height) == (4, 64, 32)


def test_source_defaults_to_gige():
    assert parse_config({}).source.type == "gige"


def test_source_type_parsed():
    assert parse_config({"source": {"type": "usb"}}).source.type == "usb"


def test_make_source_unknown_raises():
    # factory dispatch is testable without a source's native deps (impls import lazily)
    try:
        make_source(parse_config({"source": {"type": "nope"}}))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_usb_config_defaults():
    u = parse_config({}).usb
    assert (u.device, u.fake, u.pixel_format) == ("/dev/video0", False, "GRAY8")


def test_usb_config_parsed():
    c = parse_config({"source": {"type": "usb"},
                      "usb": {"fake": True, "width": 640, "height": 480, "device": "/dev/video2"}})
    assert c.source.type == "usb"
    assert c.usb.fake is True and (c.usb.width, c.usb.height) == (640, 480)
    assert c.usb.device == "/dev/video2"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
