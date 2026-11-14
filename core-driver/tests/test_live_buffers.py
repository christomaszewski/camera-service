"""Live buffer ownership and encoded recording without an unused decode branch."""
import csv
import gc
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from cam_driver.config import parse_config
from cam_driver.gst_frame import GstFrame
from cam_driver.pipeline import CapturePipeline
from cam_driver.sources.shmsrc import ShmSource
from cam_driver.sources.usb import UsbSource
from cam_driver.sources.rtsp import RtspSource
from cam_driver.transport import FrameHeader

Gst.init(None)


def _config(tmp_path, **overrides):
    cfg = {"camera": {"type": "usb"},
           "usb": {"fake": True, "pixel_format": "MJPEG", "width": 64, "height": 48,
                   "frame_rate": 30},
           "recording": {"output_dir": str(tmp_path), "encoder": "auto"},
           "preview": {"enabled": False},
           "transport": {"raw_endpoint": {"enabled": False},
                         "plugin_endpoint": {"enabled": False}}}
    cfg.update(overrides)
    return parse_config(cfg)


def test_owned_live_frames_release_a_one_slot_capture_pool():
    pool = Gst.BufferPool.new()
    config = pool.get_config()
    Gst.BufferPool.config_set_params(config, None, 16, 1, 1)
    assert pool.set_config(config) and pool.set_active(True)
    params = Gst.BufferPoolAcquireParams()
    params.flags = Gst.BufferPoolAcquireFlags.DONTWAIT
    retained, original = [], None
    try:
        # More retained downstream frames than even the recorder queue can hold.
        for i in range(40):
            flow, original = pool.acquire_buffer(params)
            assert flow == Gst.FlowReturn.OK, "downstream retained a capture-pool slot"
            original.fill(0, bytes([i]) * 16)
            retained.append(GstFrame.owned_copy(original).to_buffer(i, i))
            original = None
            gc.collect()
        assert [b.extract_dup(0, 16) for b in retained] == [bytes([i]) * 16 for i in range(40)]
    finally:
        original = None
        gc.collect()
        pool.set_active(False)


@pytest.mark.parametrize("pixel_format", ["GRAY8", "MJPEG"])
def test_live_buffer_start_reconnect_and_transform_byte_fallback(tmp_path, pixel_format):
    cfg = _config(tmp_path)
    cfg.usb.pixel_format = pixel_format
    src = UsbSource(cfg.usb)
    retained, expected = [], []
    needs_bytes = False
    ready = threading.Event()

    def receive(_stamp, data):
        retained.append(data)
        expected.append(bytes(data))
        if len(retained) >= 4:
            ready.set()

    try:
        src.open()
        src.configure()
        src.start_buffers(receive, bytes_required=lambda: needs_bytes)
        assert ready.wait(5)
        src.stop()
        assert all(isinstance(f, GstFrame) for f in retained)
        assert [bytes(f) for f in retained] == expected
        # Recording settings can turn on a Python pixel transform between sessions.
        needs_bytes = True
        ready.clear()
        before = len(retained)
        src.reopen()
        src.start_buffers(receive, bytes_required=lambda: needs_bytes)
        assert ready.wait(5)
        src.stop()
        assert len(retained) > before and all(isinstance(f, bytes) for f in retained[before:])
        assert [bytes(f) for f in retained] == expected
    finally:
        src.stop()


def test_rtsp_without_raw_output_keeps_encoded_timestamp_probe():
    cfg = parse_config({"rtsp": {"probe": False, "codec": "h264", "width": 64, "height": 48}})
    src = RtspSource(cfg.rtsp)
    try:
        src.open()
        src.set_raw_output_required(False)
        src.configure()
        assert src._rawsink is None and src._encsink is not None
        tee = src._pipeline.get_by_name("st")
        assert tee.get_static_pad("sink").num_probes > 0
    finally:
        src.stop()


@pytest.mark.parametrize("framing", ["raw", "header", "unixfd"])
@pytest.mark.parametrize("owned", [False, True])
def test_shm_payload_and_stamps_survive_source_release(framing, owned):
    cfg = parse_config({"shm": {"framing": framing, "pixel_format": "RGB", "width": 4, "height": 2}})
    src = ShmSource(cfg.shm)
    src._owned_buffer_frames = owned
    received = []
    src._on_frame = lambda stamp, frame: received.append((stamp, frame))
    pixels, timestamp = bytes(range(24)), 1_700_000_000_000_000_000
    header = (FrameHeader(timestamp, 7, 4, 2, "RGB", "camera").pack()
              if framing == "header" else b"")
    buf = Gst.Buffer.new_wrapped(header + pixels)
    buf.offset, buf.offset_end = 7, timestamp
    caps = Gst.Caps.from_string("video/x-raw,format=RGB,width=4,height=2")
    sample = Gst.Sample.new(buf, caps, None, None)
    assert src._on_raw(SimpleNamespace(emit=lambda _: sample)) == Gst.FlowReturn.OK
    sample = buf = None
    gc.collect()
    stamp, frame = received[0]
    assert isinstance(frame, GstFrame if owned else bytes)
    assert bytes(frame) == pixels
    if framing != "raw":
        assert stamp.timestamp_ns == timestamp and stamp.frame_id == 7


@pytest.mark.parametrize("reader", ["none", "preview", "raw", "plugin", "reencode"])
def test_decode_branch_follows_resolved_recording_and_output_demand(tmp_path, reader):
    cfg = _config(tmp_path)
    cfg.preview.enabled = reader == "preview"
    cfg.preview.sink = "fakesink sync=false"
    cfg.transport.raw_endpoint.enabled = reader == "raw"
    cfg.transport.raw_endpoint.socket_path = str(tmp_path / "raw")
    cfg.transport.plugin_endpoint.enabled = reader == "plugin"
    cfg.transport.plugin_endpoint.socket_path = str(tmp_path / "plugin")
    if reader == "reencode":
        cfg.recording.encoder = "ffv1"
    source = UsbSource(cfg.usb)
    pipe = CapturePipeline(cfg, source)
    try:
        source.open()
        source.configure()
        pipe.build()
        assert (source._rawsink is None) == (reader == "none")
        source.reopen()
        assert (source._rawsink is None) == (reader == "none")
    finally:
        source.stop()
        if pipe.pipeline is not None:
            pipe.pipeline.set_state(Gst.State.NULL)


def test_stream_copy_without_decode_records_exact_frames_and_sidecar(tmp_path):
    class FiniteUsb(UsbSource):
        def _pipeline_desc(self):
            return super()._pipeline_desc().replace("videotestsrc is-live=true",
                                                   "videotestsrc num-buffers=12 is-live=true")

    cfg = _config(tmp_path)
    source = FiniteUsb(cfg.usb)
    pipe = CapturePipeline(cfg, source)
    encoded = []

    def record(stamp, data, caps):
        encoded.append((stamp, data))
        pipe._on_encoded(stamp, data, caps)

    try:
        source.open()
        source.configure()
        pipe.build()
        assert source._rawsink is None and pipe._stream_copy
        assert pipe.activate()["ok"]
        source.start_buffers(pipe._on_frame, record)
        msg = source._pipeline.get_bus().timed_pop_filtered(
            10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert msg is not None and msg.type == Gst.MessageType.EOS
        source.stop()
        assert pipe.deactivate()["ok"]
        assert len(encoded) == pipe.drops.frames == 12 and source._raw_delivered == 0
        with next(tmp_path.glob("*.csv")).open() as stream:
            rows = list(csv.DictReader(stream))
        assert [int(r["timestamp_ns"]) for r in rows] == [st.timestamp_ns for st, _ in encoded]
        assert [int(r["frame_id"]) for r in rows] == [st.frame_id for st, _ in encoded]
        output = []
        for part in sorted(tmp_path.glob("*.mkv")):
            reader = Gst.parse_launch(f'filesrc location="{part}" ! matroskademux ! appsink name=sink sync=false')
            reader.set_state(Gst.State.PLAYING)
            try:
                while sample := reader.get_by_name("sink").emit("try-pull-sample", 5 * Gst.SECOND):
                    buf = sample.get_buffer()
                    output.append(buf.extract_dup(0, buf.get_size()))
            finally:
                reader.set_state(Gst.State.NULL)
        assert output == [data for _, data in encoded]
    finally:
        source.stop()
        pipe.deactivate()
        if pipe.pipeline is not None:
            pipe.pipeline.set_state(Gst.State.NULL)
