"""Fail the image build on mixed media-path plugins, missing typelibs, or missing dependencies."""
import gi

for namespace in ("Gst", "GstApp", "GstVideo", "GstAllocators", "GstRtspServer"):
    gi.require_version(namespace, "1.0")
from gi.repository import Gst, GstApp, GstVideo, GstAllocators, GstRtspServer

Gst.init(None)
assert Gst.version()[:3] == (1, 28, 7), Gst.version_string()
assert hasattr(GstApp.AppSink, "set_simple_callbacks")
assert hasattr(GstAllocators, "ShmAllocator")
for name in ("appsrc", "appsink", "videoconvert", "videotestsrc", "matroskademux",
             "matroskamux", "splitmuxsrc", "splitmuxsink", "unixfdsink", "unixfdsrc",
             "shmsink", "shmsrc", "v4l2src", "rtspsrc", "rtph264depay", "h264parse",
             "jpegparse", "jpegdec", "avdec_ffv1", "avenc_ffv1", "avdec_h264", "x264enc",
             "bayer2rgb", "textoverlay", "webrtcbin", "dtlssrtpenc", "sctpenc"):
    factory = Gst.ElementFactory.find(name)
    assert factory is not None, f"missing element: {name}"
    plugin = factory.get_plugin()
    assert plugin.get_version() == "1.28.7", (name, plugin.get_version(), plugin.get_filename())
if Gst.ElementFactory.find("webrtcsink") is not None:
    for name in ("webrtcsrc", "nicesrc", "nicesink", "rtpgccbwe"):
        assert Gst.ElementFactory.make(name) is not None, f"missing bridge dependency: {name}"
print("GStreamer 1.28.7: media plugins and Python typelibs verified")
