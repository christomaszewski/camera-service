"""Build the real transport and verify its advertised rate matches decimation, not recording.

Run explicitly in webrtc-bridge:dev (GStreamer 1.24+, with unixfd and GstAllocators).
Kept outside test_*.py unit collection, whose runner does not ship these media plugins.
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cam_driver.config import AppConfig
from cam_driver.pipeline import CapturePipeline, Gst


def test_native_transport_caps_describe_the_decimated_rate():
    Gst.init(None)
    assert Gst.ElementFactory.find("unixfdsink"), "run in the GStreamer 1.24+ dev image"
    for pixel_format, expected_format in [("Mono8", "video/x-raw"), ("BayerRG8", "video/x-bayer")]:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig()
            cfg.recording.enabled = False
            cfg.camera.frame_rate = 24
            cfg.transport.plugin_endpoint.socket_path = os.path.join(tmp, "frames")
            cfg.transport.plugin_endpoint.max_rate_hz = 10
            source = SimpleNamespace(geometry=lambda: (0, 0, 16, 16), pixel_format=lambda: pixel_format,
                                     encoded_caps=None)
            pipeline = CapturePipeline(cfg, source)
            pipeline.build()
            try:
                caps = pipeline.unixfd_src.get_property("caps").to_string()
                assert caps.startswith(expected_format), caps
                assert "framerate=(fraction)8/1" in caps, caps
                assert "framerate=(fraction)24/1" in pipeline.appsrc.get_property("caps").to_string()
            finally:
                pipeline.pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    test_native_transport_caps_describe_the_decimated_rate()
    print("ok test_native_transport_caps_describe_the_decimated_rate")
