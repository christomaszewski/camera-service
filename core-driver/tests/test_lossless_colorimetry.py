"""Mono/Bayer recordings stay bit-exact across the YUV colour step, above GStreamer's 576-row cutoff.

hw-hevc-lossless and x265-lossless carry a GRAY8/Bayer8 plane in the Y plane of a YUV container.
Unpinned, GStreamer gives that container its height-based default colorimetry (limited range
above 576 rows), and videoconvert squeezes 256 codes into 220. The JetPack validation ran at
512x512 and never crossed that line, so 5 MP recordings lost up to 3 codes per pixel. Every case
here runs at 600 rows, on each CI runtime (GStreamer 1.20 / 1.24 / 1.28), with real elements.
"""
from pathlib import Path
import importlib.util
import random
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo
import pytest

import cam_driver.recorder as rec
from cam_driver.config import RecordingConfig
from cam_driver.formats import LUMA_COLORIMETRY
from cam_driver.sources.replay import legacy_luma_note, raw_decode_chain

Gst.init(None)
W, H = 64, 600                    # > 576 rows: the height that selects GStreamer's HD defaults
GRAY = f"video/x-raw,format=GRAY8,width={W},height={H},framerate=25/1"
INTERVAL = Gst.SECOND // 25


def _frames(n):
    """A ramp holding every code, then near-incompressible noise."""
    ramp = (bytes(range(256)) * (W * H // 256 + 1))[:W * H]
    return [ramp] + [random.Random(i).randbytes(W * H) for i in range(1, n)]


def _luma(sample):
    """The Y (or GRAY8) plane of a decoded sample, stride-corrected."""
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    buf = sample.get_buffer()
    meta = GstVideo.buffer_get_video_meta(buf)
    offset, stride = (meta.offset[0], meta.stride[0]) if meta else (info.offset[0], info.stride[0])
    ok, mapped = buf.map(Gst.MapFlags.READ)
    assert ok
    try:
        data = bytes(mapped.data)
    finally:
        buf.unmap(mapped)
    return b"".join(data[offset + r * stride: offset + r * stride + W] for r in range(H))


def _run(desc, frames=(), caps=GRAY):
    """Push `frames` through `appsrc ! desc` (when frames given) and return the planes an
    `appsink name=sink` collected through EOS. Raises on a pipeline ERROR."""
    head = f'appsrc name=src format=time caps="{caps}" ! ' if frames else ""
    pipeline = Gst.parse_launch(head + desc)
    sink, bus = pipeline.get_by_name("sink"), pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    out = []
    try:
        if frames:
            src = pipeline.get_by_name("src")
            for i, pixels in enumerate(frames):
                buf = Gst.Buffer.new_wrapped(pixels)
                buf.pts, buf.duration = i * INTERVAL, INTERVAL
                assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
            src.emit("end-of-stream")
        # appsink holds EOS until its queue is pulled, so drain while waiting for it.
        for _ in range(200):
            if sink is not None:
                sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
                if sample is not None:
                    out.append(_luma(sample))
                    continue
            msg = bus.timed_pop_filtered(0 if sink is not None else 100 * Gst.MSECOND,
                                         Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is not None and msg.type == Gst.MessageType.ERROR:
                raise AssertionError(msg.parse_error())
            if (msg is not None) or (sink is not None and sink.get_property("eos")):
                break
        else:
            raise AssertionError("pipeline neither finished nor failed in 20 s")
    finally:
        pipeline.set_state(Gst.State.NULL)
    return out


def _decode(path, chain):
    return _run(f'filesrc location="{path}" ! matroskademux ! {chain} ! videoconvert ! '
                'video/x-raw,format=GRAY8 ! appsink name=sink sync=false')


@pytest.fixture(scope="module")
def x265_recording(tmp_path_factory):
    """A real x265-lossless recording written by the recorder's own launch fragment."""
    base = tmp_path_factory.mktemp("x265") / "rec"
    desc, enc = rec.build_recorder_description(RecordingConfig(encoder="x265-lossless"), 8, str(base))
    assert enc == "x265-lossless", "CI images ship x265enc; this case must not degrade to ffv1"
    frames = _frames(4)
    _run(desc, frames)
    header = {"recording_encoder": enc, "recording_colorimetry": rec.luma_colorimetry(enc, False)}
    return Path(f"{base}-00000.mkv"), frames, header


def test_nvenc_colour_step_copies_the_plane_into_y(monkeypatch):
    # The recorder's real hw-hevc fragment up to nvvidconv: the CPU colour step is the only part
    # that can change values (NVENC lossless stores NV24 samples verbatim). No NVENC needed.
    monkeypatch.setattr(rec, "_resolve_available", lambda enc: enc)
    desc, enc = rec.build_recorder_description(RecordingConfig(encoder="hw-hevc-lossless"), 8, "/unused")
    assert enc == "hw-hevc-lossless"
    colour_step = desc.split(" ! nvvidconv")[0]
    assert f"format=NV24,colorimetry={LUMA_COLORIMETRY}" in colour_step
    frames = _frames(3)
    assert _run(colour_step + " ! appsink name=sink sync=false", frames) == frames


def test_x265_lossless_round_trips_through_the_replay_decode(x265_recording):
    path, frames, header = x265_recording
    assert header["recording_colorimetry"] == LUMA_COLORIMETRY
    assert _decode(path, raw_decode_chain("video/x-h265", header)) == frames


def test_decode_ignores_the_range_the_encoder_signalled(tmp_path):
    # NVENC's VUI is not relied on: a stream whose bitstream says LIMITED range but whose Y plane
    # holds full-range codes still decodes verbatim once the sidecar's colorimetry is re-asserted.
    frames = _frames(3)
    neutral = bytes([128]) * (W * H)
    y444 = [f + neutral + neutral for f in frames]
    path = tmp_path / "limited.mkv"
    _run('x265enc option-string="lossless=1" speed-preset=ultrafast ! h265parse ! matroskamux ! '
         f'filesink location="{path}"', y444,
         caps=f"video/x-raw,format=Y444,width={W},height={H},framerate=25/1,colorimetry=bt709")
    pinned = {"recording_colorimetry": LUMA_COLORIMETRY}
    assert _decode(path, raw_decode_chain("video/x-h265", pinned)) == frames
    # Without the override the decode follows the signalled range -- the case this guards against.
    assert _decode(path, raw_decode_chain("video/x-h265", {})) != frames


def test_hardware_validator_decode_runs_and_is_exact(x265_recording, tmp_path):
    # nvenc_lossless_test.py's gst-launch decode (capssetter argument quoting included) on a real
    # HEVC recording; its encode needs the Orin and is covered by the colour-step case above.
    path, frames, _header = x265_recording
    spec = importlib.util.spec_from_file_location(
        "nvenc_validation", Path(__file__).resolve().parents[1] / "tools/nvenc_lossless_test.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.W, tool.H, tool.IMG = W, H, W * H
    tool.ENC, tool.OUT = str(path), str(tmp_path / "out.raw")
    result = subprocess.run(tool.decode_argv(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert Path(tool.OUT).read_bytes() == b"".join(frames)


def test_raw_decode_chain_validates_the_sidecar_value():
    assert raw_decode_chain("video/x-h265", {}) == "h265parse ! avdec_h265"
    assert raw_decode_chain("video/x-h265", {"recording_colorimetry": None}) == "h265parse ! avdec_h265"
    pinned = raw_decode_chain("video/x-h265", {"recording_colorimetry": LUMA_COLORIMETRY})
    assert pinned.startswith("h265parse ! avdec_h265 ! capssetter ") and LUMA_COLORIMETRY in pinned
    with pytest.raises(ValueError, match="unsupported recording_colorimetry"):
        raw_decode_chain("video/x-h265", {"recording_colorimetry": '1:4:0:0" ! fakesink'})


@pytest.mark.parametrize("header, flagged", [
    ({"recording_encoder": "hw-hevc-lossless", "pixel_format": "BayerRG8", "height": 2048}, True),
    ({"recording_encoder": "x265-lossless", "pixel_format": "Mono8", "height": 600}, True),
    ({"recording_encoder": "hw-hevc-lossless", "pixel_format": "BayerRG8", "height": 576}, False),
    ({"recording_encoder": "hw-hevc-lossless", "pixel_format": "BayerRG8", "height": 2048,
      "recording_colorimetry": LUMA_COLORIMETRY}, False),
    ({"recording_encoder": "ffv1", "pixel_format": "BayerRG8", "height": 2048}, False),
    ({"recording_encoder": "x265-lossless", "pixel_format": "I420", "height": 2048}, False),
    ({"recording_encoder": None, "pixel_format": "Mono8", "height": 2048}, False),
])
def test_legacy_recordings_are_flagged_only_where_the_squeeze_applied(header, flagged):
    assert (legacy_luma_note(header) is not None) is flagged
