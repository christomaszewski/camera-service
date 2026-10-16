"""FFV1 decoder EOS fidelity, including Ubuntu 24.04's gst-libav 1.24.2.

Its automatic frame threading can consume every packet yet discard the final decoded
frame. Check actual pixels, timestamps and counts using the replay decoder policy.
"""
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import pytest

from cam_driver.formats import FFV1_DECODER, bytes_per_frame
from test_recording_h264 import collect

Gst.init(None)


@pytest.mark.parametrize("pixel_format", ["GRAY8", "GRAY16_LE", "I420", "NV12"])
@pytest.mark.parametrize("count", [1, 5, 32])
@pytest.mark.parametrize("sliced", [False, True], ids=["legacy-unsliced", "four-slices"])
def test_ffv1_eos_preserves_every_pixel_and_timestamp(tmp_path, pixel_format, count, sliced):
    width, height, interval = 64, 48, 40_000_000
    caps = f"video/x-raw,format={pixel_format},width={width},height={height},framerate=25/1"
    frames = [random.Random(i).randbytes(bytes_per_frame(pixel_format, width, height))
              for i in range(count)]
    path = tmp_path / "recording.mkv"
    options = "coder=1 context=1 threads=0 slices=4 slicecrc=1" if sliced else ""
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time caps="{caps}" ! videoconvert ! '
        f'avenc_ffv1 {options} ! matroskamux ! filesink location="{path}"')
    src = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for i, pixels in enumerate(frames):
            buf = Gst.Buffer.new_wrapped(pixels)
            buf.pts, buf.duration = i * interval, interval
            assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        assert src.emit("end-of-stream") == Gst.FlowReturn.OK
        msg = pipeline.get_bus().timed_pop_filtered(
            10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert msg and msg.type == Gst.MessageType.EOS, msg.parse_error() if msg else "encode timeout"
    finally:
        pipeline.set_state(Gst.State.NULL)

    decoded = collect(f'filesrc location="{path}" ! matroskademux ! '
                      f'{FFV1_DECODER} ! videoconvert ! {caps}')
    assert decoded == [(i * interval, pixels) for i, pixels in enumerate(frames)]
