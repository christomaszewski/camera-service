#!/usr/bin/env python3
"""Tiny RTSP server for tests -- serves videotestsrc as MJPEG over RTP at
rtsp://0.0.0.0:8554/test. No camera/hardware; lets rtsp_test.sh exercise the RtspSource
end to end. Needs gir1.2-gst-rtsp-server-1.0 (in the cam-dev image).

Usage: python3 fake_rtsp_server.py [width] [height] [fps]
"""
import sys

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer

W = int(sys.argv[1]) if len(sys.argv) > 1 else 512
H = int(sys.argv[2]) if len(sys.argv) > 2 else 512
FPS = int(sys.argv[3]) if len(sys.argv) > 3 else 25

Gst.init(None)
server = GstRtspServer.RTSPServer()
server.set_service("8554")
factory = GstRtspServer.RTSPMediaFactory()
# format=I420 is REQUIRED: videotestsrc's default sampling makes jpegenc emit a JPEG whose SOF
# rtpjpegpay can't parse ("Invalid component" -> no RTP, DESCRIBE hangs). 4:2:0 (RFC2435 type 1) works.
factory.set_launch(
    f"( videotestsrc is-live=true ! video/x-raw,format=I420,width={W},height={H},framerate={FPS}/1 "
    f"! jpegenc ! rtpjpegpay name=pay0 pt=96 )")
factory.set_shared(True)
server.get_mount_points().add_factory("/test", factory)
server.attach(None)
print(f"fake RTSP server up: rtsp://127.0.0.1:8554/test (MJPEG {W}x{H}@{FPS})", flush=True)
GLib.MainLoop().run()
