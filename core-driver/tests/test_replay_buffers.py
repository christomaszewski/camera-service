"""Replay buffer ownership, transport, and recording regressions with real GStreamer.

Run as a standalone script in cam-dev (1.20/shm) and cam-dev:jp6m (1.28/unixfd).
The legacy live-source byte seam is exercised alongside the opt-in replay seam.
"""
import gc
import os
import sys
import tempfile
import time
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from cam_driver.config import parse_config
from cam_driver.gst_frame import GstFrame
from cam_driver.pipeline import CapturePipeline, _new_buffer
from cam_driver.session import PushResult
from cam_driver.timestamps import FrameStamp, TimestampSource
from cam_driver.transport import unpack_header, HEADER_SIZE
from test_replay_source import _write_session, _source, _pump, _px, T0, IV, W, H

Gst.init(None)


def _read(buf):
    return buf.extract_dup(0, buf.get_size())


def test_buffers_share_pixels_but_have_independent_metadata_and_lifetimes():
    original = Gst.Buffer.new_wrapped(b"original pixels")
    original.pts, original.dts, original.offset, original.duration = 99, 88, 77, 66
    original.set_flags(Gst.BufferFlags.DISCONT)
    frame = GstFrame(original)
    a, b = frame.to_buffer(10, 1), frame.to_buffer(20, 2)
    assert a.peek_memory(0) == original.peek_memory(0) == b.peek_memory(0)
    assert (original.pts, original.dts, original.offset, original.duration) == (99, 88, 77, 66)
    assert (a.pts, a.offset, b.pts, b.offset) == (10, 1, 20, 2)
    assert a.dts == a.duration == Gst.CLOCK_TIME_NONE
    assert not a.has_flags(Gst.BufferFlags.DISCONT)
    hdr = frame.to_buffer(30, 3, prefix=b"header")
    del frame, original
    gc.collect()
    assert _read(a) == _read(b) == b"original pixels"
    assert _read(hdr) == b"headeroriginal pixels"


def test_memfd_copy_has_exact_pixels():
    payload = bytes(range(256)) * 100
    frame = GstFrame(Gst.Buffer.new_wrapped(payload))
    with tempfile.TemporaryFile() as f:
        frame.write_to_fd(f.fileno())
        assert f.read() == payload


def test_transport_buffer_keeps_decoder_pool_frame_until_consumer_releases_it():
    pool = Gst.BufferPool.new()
    config = pool.get_config()
    Gst.BufferPool.config_set_params(config, None, 16, 1, 1)
    assert pool.set_config(config) and pool.set_active(True)
    original = child = available = None
    try:
        flow, original = pool.acquire_buffer(None)
        assert flow == Gst.FlowReturn.OK
        original.fill(0, b"0123456789abcdef")
        child = GstFrame(original).to_buffer(1, 2, prefix=b"header")
        original = None
        gc.collect()
        params = Gst.BufferPoolAcquireParams()
        params.flags = Gst.BufferPoolAcquireFlags.DONTWAIT
        flow, available = pool.acquire_buffer(params)
        assert flow != Gst.FlowReturn.OK, "the consumer still owns the pool's only frame"
        assert _read(child) == b"header0123456789abcdef"
        child = None
        gc.collect()
        flow, available = pool.acquire_buffer(params)
        assert flow == Gst.FlowReturn.OK, "the decoder can reuse the frame after delivery"
    finally:
        original = child = available = None
        gc.collect()
        pool.set_active(False)


def test_existing_byte_buffer_metadata_and_live_start_contract():
    calls = []
    source = SimpleNamespace(start=lambda *callbacks: calls.append(callbacks))
    p = CapturePipeline(parse_config({}), source)
    p._start_source()
    assert calls == [(p._on_frame, p._on_encoded)]
    assert p._discard_replay_main is False
    b = _new_buffer(b"live pixels", 123, 456)
    assert _read(b) == b"live pixels" and b.pts == 123 and b.offset == 456
    assert b.dts == b.duration == Gst.CLOCK_TIME_NONE


def test_recording_tiler_gets_bytes_from_a_buffer_frame():
    p = CapturePipeline(parse_config({}), SimpleNamespace())
    seen = []

    def tile(data):
        assert isinstance(data, bytes)
        seen.append(data)
        return data[::-1]

    def push(data, pts, stamp, caps):
        assert data == b"dcba" and pts == 123
        return PushResult.OK

    p._tiler = tile
    frame = GstFrame(Gst.Buffer.new_wrapped(b"abcd"))
    assert p._feed_recording(SimpleNamespace(push=push), None, 123, frame)
    assert seen == [b"abcd"]


def test_rate_limited_and_backpressured_frames_do_not_materialize_pixels():
    calls = []

    class GuardedFrame(GstFrame):
        def __bytes__(self):
            calls.append("bytes")
            raise AssertionError("skipped pixels must not be extracted")

        def to_buffer(self, *args, **kwargs):
            calls.append("buffer")
            raise AssertionError("skipped pixels must not be wrapped")

    cfg = parse_config({"transport": {"plugin_endpoint": {"max_rate_hz": 12.5}}})
    p = CapturePipeline(cfg, SimpleNamespace(take_discontinuity=lambda: False))
    p._discard_replay_main = True
    p._fps, p._pub_seq = 25.0, 1  # every other frame; this one is skipped
    frame = GuardedFrame(Gst.Buffer.new_wrapped(bytes(W * H)))
    stamp = FrameStamp(frame_id=0, timestamp_ns=T0, source=TimestampSource.SYSTEM,
                       system_ns=T0, camera_ns=T0, chunk_ns=None)
    p._on_frame(stamp, frame)
    assert calls == [] and p._still is None and p.drops.frames == 1
    # A selected frame whose transport appsrc is full must be rejected before buffer assembly.
    p.transport_src = SimpleNamespace(get_property=lambda key: {
        "max-bytes": 1, "current-level-bytes": 1, "block": False}[key])
    p._width, p._height = W, H
    p._publish_transport(stamp, frame, 0)
    assert calls == [] and p.drops.publish_drops == 1


def test_source_retains_buffers_across_sessions_restart_and_reader_teardown():
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        _write_session(run, "a", 1, T0, 4)
        _write_session(run, "b", 2, T0 + 4 * IV, 4, frame_id0=10)
        src = _source(run)
        frames = []
        try:
            src.start_buffers(lambda st, data: frames.append((st, data)), shared_memory=True)
            _pump(lambda: src.finished)
            assert src.playback.request("restart")["ok"]
            _pump(lambda: src.finished and len(frames) == 16)
        finally:
            src.stop()
        gc.collect()
        assert all(isinstance(data, GstFrame) for _, data in frames)
        assert [st.frame_id for st, _ in frames] == [0, 1, 2, 3, 10, 11, 12, 13] * 2
        expected = [_px(sid, i) for sid in (1, 2) for i in range(4)] * 2
        assert [bytes(data) for _, data in frames] == expected


def test_buffer_source_preserves_window_pause_and_byte_fallback_for_untile():
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        _write_session(run, "a", 1, T0, 6)
        src = _source(run, initial_state="paused", from_s=0.08, to_s=0.20)
        frames = []
        try:
            src.start_buffers(lambda st, data: frames.append((st, data)))
            _pump(lambda: len(frames) == 1)
            time.sleep(0.1)
            assert len(frames) == 1 and frames[0][0].frame_id == 2
            assert src.playback.request("resume")["ok"]
            _pump(lambda: src.finished)
            assert [st.frame_id for st, _ in frames] == [2, 3, 4]
        finally:
            src.stop()
        src = _source(run)
        frames = []

        def untile(data):
            assert isinstance(data, bytes)
            return bytes(v ^ 0xff for v in data)

        src._untile = untile
        try:
            src.start_buffers(lambda st, data: frames.append(data))
            _pump(lambda: src.finished)
            assert frames == [untile(_px(1, i)) for i in range(6)]
        finally:
            src.stop()


def _exercise_pipeline(recording, raw_endpoint=False, nv12=False, preview=False,
                       shared_allocation=True, legacy_unixfd=False):
    """Real reader -> core -> transport consumer, optionally recording + raw consumer in parallel."""
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp) / "input"
        run.mkdir()
        def pixels(i):
            if nv12:
                return bytes([50 + i * 7]) * (W * H) + bytes([64 + i, 192 - i]) * (W * H // 4)
            return _px(1, i)

        original = _write_session(run, "a", 1, T0, 10,
                                  pixel_format="NV12" if nv12 else "GRAY8", pixels=pixels)
        out = Path(tmp) / "output"
        cfg = parse_config({
            "camera": {"type": "replay"}, "replay": {"path": str(run), "speed": 1.0},
            "recording": {"enabled": True, "encoder": "ffv1", "output_dir": str(out)},
            "preview": {"enabled": preview, "sink": "appsink name=preview emit-signals=true sync=false"},
            "control": {"enabled": False},
            "transport": {"plugin_endpoint": {"enabled": True, "socket_path": tmp + "/frames",
                                               "max_rate_hz": 12.5},
                          "raw_endpoint": {"enabled": raw_endpoint, "socket_path": tmp + "/raw"}}})
        src = _source(run, speed=1.0)
        pipe = CapturePipeline(cfg, src)
        pipe.build()
        if legacy_unixfd:
            pipe._unixfd_pool = False   # exercise the pre-1.28 transport fallback on modern CI
        if not shared_allocation:
            src._propose_allocation = lambda sink, query: False
        source_frames = []
        on_frame = pipe._on_frame

        def observe_frame(stamp, data):
            source_frames.append(data)  # retain beyond decoder teardown to catch pool reuse
            on_frame(stamp, data)

        pipe._on_frame = observe_frame
        assert pipe._discard_replay_main is (not raw_endpoint and not preview)
        consumer = None
        raw_consumer = None
        received, raw_frames, preview_frames = [], [], []
        try:
            if preview:
                def on_preview(sink):
                    preview_frames.append(_read(sink.emit("pull-sample").get_buffer()))
                    return Gst.FlowReturn.OK

                pipe.pipeline.get_by_name("preview").connect("new-sample", on_preview)
            pipe.pipeline.set_state(Gst.State.PLAYING)
            endpoint = (f'unixfdsrc socket-path="{pipe._unixfd_path}"' if pipe._have_unixfd else
                        f'shmsrc socket-path="{tmp}/frames" is-live=true do-timestamp=true')
            consumer = Gst.parse_launch(endpoint + ' ! appsink name=sink emit-signals=true sync=false')

            def on_sample(sink):
                buf = sink.emit("pull-sample").get_buffer()
                data = _read(buf)
                if pipe._have_unixfd:
                    received.append((buf.offset, buf.offset_end, data, buf.pts))
                else:
                    header = unpack_header(data)
                    received.append((header.frame_id, header.timestamp_ns, data[HEADER_SIZE:], buf.pts))
                return Gst.FlowReturn.OK

            consumer.get_by_name("sink").connect("new-sample", on_sample)
            consumer.set_state(Gst.State.PLAYING)
            if raw_endpoint:
                raw_consumer = Gst.parse_launch(
                    f'shmsrc socket-path="{tmp}/raw" is-live=true ! appsink name=sink emit-signals=true sync=false')

                def on_raw(sink):
                    raw_frames.append(_read(sink.emit("pull-sample").get_buffer()))
                    return Gst.FlowReturn.OK

                raw_consumer.get_by_name("sink").connect("new-sample", on_raw)
                raw_consumer.set_state(Gst.State.PLAYING)
            time.sleep(0.1)  # let the local socket clients connect before the first frame
            if recording:
                assert pipe.activate()["ok"]
            # Native pooling must avoid Python memfd allocation even when upstream declines
            # shared memory. The C pool handles that fallback, with identical output metadata.
            with patch("cam_driver.pipeline.os.memfd_create", wraps=os.memfd_create) as create_fd:
                pipe._start_source()
                _pump(lambda: src.finished and len(received) >= 5)
                if pipe._unixfd_pool:
                    assert create_fd.call_count == 0
                elif pipe._have_unixfd:
                    assert create_fd.call_count == 5
            if pipe._unixfd_pool and shared_allocation:
                from gi.repository import GstAllocators
                assert len(source_frames) == 10
                assert all(GstAllocators.is_fd_memory(frame._buffer.peek_memory(i))
                           for frame in source_frames for i in range(frame._buffer.n_memory()))
            assert [bytes(frame) for frame in source_frames] == [pixels(i) for i in range(10)]
            assert [r[0] for r in received] == [0, 2, 4, 6, 8]
            assert [r[1] for r in received] == [T0 + i * IV for i in range(0, 10, 2)]
            assert [r[2] for r in received] == [pixels(i) for i in range(0, 10, 2)]
            if raw_endpoint:
                _pump(lambda: len(raw_frames) == 10)
                assert raw_frames == [pixels(i) for i in range(10)]
            elif not recording:
                assert not list(out.glob("*.mkv")) and pipe._session is None
            if preview:
                _pump(lambda: len(preview_frames) == 10)
                assert preview_frames == [pixels(i) for i in range(10)]
            src.stop()
            # Retain and re-publish a real decoded frame after tearing its reader down.
            count = pipe._session.frames if pipe._session else 0
            pipe._still_pushed_mono = 0
            pipe._still_tick()
            _pump(lambda: len(received) >= 6)
            assert received[-1][:3] == received[-2][:3]
            assert received[-1][3] > received[-2][3]
            assert (pipe._session.frames if pipe._session else 0) == count
            if recording:
                assert count == 10 and pipe.deactivate()["ok"]
                assert next(out.glob("*.csv")).read_text() == original.with_suffix(".csv").read_text()
                reread = _source(out)
                recorded_pixels = []
                try:
                    reread.start(lambda st, data: recorded_pixels.append(data))
                    _pump(lambda: reread.finished)
                    assert recorded_pixels == [pixels(i) for i in range(10)]
                finally:
                    reread.stop()
            assert pipe.drops.enqueue_failures == 0 and pipe.drops.publish_drops == 0
        finally:
            for c in (consumer, raw_consumer):
                if c is not None:
                    c.set_state(Gst.State.NULL)
            pipe.shutdown()


def test_inactive_replay_streams_with_no_recording_or_unused_main_feed():
    _exercise_pipeline(recording=False)


def test_replay_records_every_frame_while_streaming_at_a_lower_rate():
    _exercise_pipeline(recording=True)


def test_replay_raw_endpoint_still_receives_every_frame():
    _exercise_pipeline(recording=True, raw_endpoint=True)


def test_nv12_replay_records_exact_pixels_and_streams_with_preview_enabled():
    _exercise_pipeline(recording=True, nv12=True, preview=True)


def test_native_transport_pool_when_upstream_declines_shared_allocation():
    _exercise_pipeline(recording=True, nv12=True, shared_allocation=False)


def test_older_unixfd_falls_back_to_python_memfd_copy():
    _exercise_pipeline(recording=True, nv12=True, legacy_unixfd=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name, flush=True)
