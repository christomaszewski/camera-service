"""Real H.264 quality, keyframe splitting and nonzero PTS regression tests (run in cam-dev)."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
import numpy as np
from cam_driver.config import RecordingConfig, parse_config
from cam_driver.recorder import build_recorder_description
from cam_driver.pipeline import CapturePipeline
from types import SimpleNamespace

Gst.init(None)
W, H, N, IV = 320, 240, 60, 40_000_000
CAPS = f'video/x-raw,format=NV12,width={W},height={H},framerate=25/1'


def collect(description):
    p = Gst.parse_launch(description + ' ! appsink name=sink sync=false max-buffers=4')
    sink = p.get_by_name('sink')
    result = []
    p.set_state(Gst.State.PLAYING)
    try:
        while True:
            sample = sink.emit('try-pull-sample', 10 * Gst.SECOND)
            if sample is None:
                msg = p.get_bus().timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
                assert msg and msg.type == Gst.MessageType.EOS, msg.parse_error() if msg else 'decode timeout'
                return result
            buf = sample.get_buffer()
            result.append((buf.pts, buf.extract_dup(0, buf.get_size())))
    finally:
        p.set_state(Gst.State.NULL)


def encode(base, frames, crf):
    cfg = RecordingConfig(encoder='x264', x264_crf=crf, segment_seconds=1, keyframe_interval_s=1)
    desc, enc = build_recorder_description(cfg, 8, str(base), 25, True)
    assert enc == 'x264'
    p = Gst.parse_launch(f'appsrc name=src format=time block=true max-bytes={W*H*6} caps="{CAPS}" ! {desc}')
    src = p.get_by_name('src')
    p.set_state(Gst.State.PLAYING)
    try:
        for i, data in enumerate(frames):
            b = Gst.Buffer.new_wrapped(data)
            b.pts, b.duration = 5 * Gst.SECOND + i * IV, IV
            assert src.emit('push-buffer', b) == Gst.FlowReturn.OK
        src.emit('end-of-stream')
        msg = p.get_bus().timed_pop_filtered(15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert msg and msg.type == Gst.MessageType.EOS, msg.parse_error() if msg else 'encode timeout'
    finally:
        p.set_state(Gst.State.NULL)
    return sorted(base.parent.glob(base.name + '-*.mkv'))


def test_quality_control_and_every_segment_decodes_with_original_pts():
    frames = [b for _, b in collect(f'videotestsrc num-buffers={N} pattern=smpte ! {CAPS}')]
    assert len(frames) == N
    original = np.frombuffer(b''.join(frames), dtype=np.uint8).astype(float)
    measurements = []
    with tempfile.TemporaryDirectory() as tmp:
        for crf in (12, 36):
            parts = encode(Path(tmp) / f'quality-{crf}', frames, crf)
            assert len(parts) == 3
            # Start a fresh decoder on EVERY part: verifies that each split starts on a keyframe.
            decoded = []
            for part in parts:
                decoded += collect(f'filesrc location="{part}" ! matroskademux ! h264parse ! avdec_h264 ! videoconvert ! {CAPS}')
            assert [pts for pts, _ in decoded] == [5 * Gst.SECOND + i * IV for i in range(N)]
            pixels = np.frombuffer(b''.join(data for _, data in decoded), dtype=np.uint8).astype(float)
            assert pixels.size == original.size
            mse = float(np.mean((pixels - original) ** 2))
            measurements.append((sum(p.stat().st_size for p in parts), mse))
    (large, high_quality_error), (small, low_quality_error) = measurements
    assert large > small * 1.3, measurements
    assert 0 < high_quality_error < low_quality_error / 2, measurements


def test_odd_geometry_is_rejected_before_starting_the_recorder():
    source = SimpleNamespace(geometry=lambda: (0, 0, 319, 240), pixel_format=lambda: 'GRAY8',
                             delivered_frame_rate=25, encoded_caps=None)
    p = CapturePipeline(parse_config({'recording': {'encoder': 'x264'}}), source)
    try:
        p.build()
    except ValueError as exc:
        assert 'even width and height' in str(exc)
    else:
        raise AssertionError('odd 4:2:0 dimensions must fail before activation')


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print('ok', name, flush=True)
