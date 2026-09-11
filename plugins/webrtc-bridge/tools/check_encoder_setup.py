"""Real webrtcsink signal/default-handler ordering; run explicitly in the WebRTC image.

This runtime check lives outside pytest's test_*.py unit collection, which has no webrtcsink.
"""
import os
from unittest.mock import patch
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
from bridge_stream import Bridge


def test_encoder_policy_survives_webrtcsink_defaults():
    Gst.init(None)
    with patch.dict(os.environ, {
        "CAM_PIPELINE": "videotestsrc name=cam_src ! video/x-raw,format=I420,width=16,height=16,framerate=10/1 ! webrtcsink name=cam_webrtcsink",
        "CAM_ADVERTISE": "0", "CAM_WEBRTC_STATUS": "0", "CAM_WIDTH": "16", "CAM_HEIGHT": "16",
        "CAM_FPS": "10", "CAM_WEBRTC_KEYFRAME_S": "2", "CAM_WEBRTC_X264_PRESET": "superfast",
    }):
        bridge = Bridge()
        bridge.build()
        try:
            sink = bridge.pipeline.get_by_name("cam_webrtcsink")
            encoder = Gst.ElementFactory.make("x264enc")
            assert encoder is not None, "x264enc required for this test"
            sink.emit("encoder-setup", "test-consumer", "video_0", encoder)
            assert encoder.get_property("key-int-max") == 20
            assert encoder.get_property("speed-preset").value_nick == "superfast"
            assert encoder.get_property("bframes") == 0
        finally:
            bridge.pipeline.get_bus().remove_signal_watch()
            bridge.pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    test_encoder_policy_survives_webrtcsink_defaults()
    print("ok test_encoder_policy_survives_webrtcsink_defaults")
