"""Real decode/encode/validate proxy tests. Run inside cam-dev without camera hardware."""
import csv
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cam_driver.config import parse_config
from cam_driver.export_recordings import _decode, _gst, canonical_hash, convert, file_hash
from cam_driver.playback import discover_sessions
from cam_driver.sources.replay import ReplaySource
from test_proxy_export import request, PROFILES, raises
from test_replay_source import _write_session, T0, IV, _Consumer, _pump


def _write_segmented_session(camera, pixel_format):
    """Real writer with jitter, a late session start and a gap at an FFV1 GOP boundary."""
    Gst = _gst()
    base = camera / "segmented"
    points = [12_345_678_900 + i * IV + (i % 7) * 240_000
              + (2_230_000_000 if i >= 36 else 0) for i in range(100)]
    pipeline = Gst.parse_launch(
        f'appsrc name=src is-live=false format=time caps="video/x-raw,format={pixel_format},'
        'width=16,height=16,framerate=25/1" ! videoconvert ! '
        'avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1 ! '
        'splitmuxsink send-keyframe-requests=true muxer=matroskamux max-size-time=250000000 '
        f'location="{base}-%05d.mkv"')
    src = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for i, pts in enumerate(points):
            size = 512 if pixel_format == "GRAY16_LE" else 256
            buf = Gst.Buffer.new_wrapped(bytes([(i * 20) % 256]) * size)
            buf.pts = pts
            assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        src.emit("end-of-stream")
        msg = pipeline.get_bus().timed_pop_filtered(
            20 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        assert msg is not None and msg.type == Gst.MessageType.EOS, msg
    finally:
        pipeline.set_state(Gst.State.NULL)
    assert len(list(camera.glob("segmented-*.mkv"))) > 2
    base.with_suffix(".json").write_text(json.dumps({
        "pixel_format": pixel_format, "width": 16, "height": 16,
        "bits_per_pixel": 16 if pixel_format == "GRAY16_LE" else 8,
        "first_timestamp_ns": T0 + points[0], "first_pts_ns": points[0],
        "base_timestamp_ns": T0, "timestamp_source": "system", "cfa_tile_mode": "off",
        "session": {"frames_recorded": len(points), "truncated": False, "error": None}}))
    with base.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frame_id", "pts_ns", "timestamp_ns", "source", "chunk_ns", "camera_ns", "system_ns"])
        for i, pts in enumerate(points):
            timestamp = T0 + pts
            writer.writerow([90 + i, pts, timestamp, "system", "", timestamp, timestamp])
    return base, points


def test_multipart_proxy_preserves_boundary_gaps_and_fractional_frame_timing():
    for pixel_format in ("GRAY8", "GRAY16_LE"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            camera = root / "recordings" / "camera"
            camera.mkdir(parents=True)
            base, points = _write_segmented_session(camera, pixel_format)
            original = {p: file_hash(p) for p in camera.iterdir()}
            output = Path(temporary) / "output"
            output.mkdir()
            req = request(root)
            # Exercise output validation across H.264 part boundaries as well.
            profiles = Path(temporary) / "profiles"
            profiles.mkdir()
            definition = req["profile"]["definition"]
            definition["encoding"]["segment_seconds"] = 0.25
            (profiles / "h264.yaml").write_text(json.dumps(definition))
            req["profile"]["sha256"] = canonical_hash(definition)
            req["destination"] = str(output)
            result = convert(req, profiles)
            assert result["recordings"][0]["sessions"][0]["validation"]["decoded_frames"] == len(points)
            assert all(file_hash(p) == digest for p, digest in original.items())
            proxy = discover_sessions(str(output / "recordings" / "camera"))[0]
            assert len(proxy.mkv_paths) > 1
            with open(proxy.csv_path) as stream, base.with_suffix(".csv").open() as source:
                assert list(csv.DictReader(stream)) == list(csv.DictReader(source))
            actual = [int(sample.get_buffer().pts) for sample in _decode(proxy)]
            assert len(actual) == len(points)
            offset = actual[0] - points[0]
            assert all(abs(got - offset - expected) <= 1_000_000
                       for got, expected in zip(actual, points))
            assert actual[36] - actual[35] > 2_000_000_000


def test_multipart_source_mismatch_at_boundary_is_still_rejected():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        camera = root / "recordings" / "camera"
        camera.mkdir(parents=True)
        base, points = _write_segmented_session(camera, "GRAY8")
        path = base.with_suffix(".csv")
        path.write_text(path.read_text().replace(f"126,{points[36]},", f"126,{points[36] + 5_000_000},"))
        output = Path(temporary) / "output"
        output.mkdir()
        req = request(root)
        req["destination"] = str(output)
        try:
            convert(req, PROFILES)
        except ValueError as exc:
            message = str(exc)
            assert "source frame 36 does not align with its CSV PTS" in message, message
            assert all(label in message for label in ("decoded PTS=", "CSV PTS=", "offset=", "delta=")), message
        else:
            raise AssertionError("timestamp corruption at a part boundary was accepted")
        assert not (output / "recordings/camera/segmented.json").exists()


def test_multi_session_proxy_preserves_stamps_geometry_selection_and_source_bytes():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        camera = root / "recordings" / "old_camera"
        camera.mkdir(parents=True)
        _write_session(camera, "early", 1, T0, 5, frame_id0=91)
        _write_session(camera, "late", 2, T0 + 3_000_000_000, 4, frame_id0=12)
        original = {p: file_hash(p) for p in camera.iterdir()}
        output = Path(temporary) / "output"
        output.mkdir()
        req = request(root, ["old_camera"])
        req["destination"] = str(output)
        result = convert(req, PROFILES)
        assert all(file_hash(p) == digest for p, digest in original.items())
        records = result["recordings"][0]["sessions"]
        assert [s["frame_count"] for s in records] == [5, 4]
        assert all(s["validation"]["stamps_preserved"] for s in records)
        assert not (output / "manifest.yaml").exists() and not (output / "bags").exists()
        for prefix in ("early", "late"):
            path = output / "recordings" / "old_camera" / prefix
            header = json.loads(path.with_suffix(".json").read_text())
            assert header["recording_encoder"] == "x264" and header["recording_lossy"] is True
            assert header["proxy"]["source_files"]
            with path.with_suffix(".csv").open() as stream, (camera / (prefix + ".csv")).open() as original_csv:
                assert list(csv.DictReader(stream)) == list(csv.DictReader(original_csv))
        cfg = parse_config({"camera": {"type": "replay"}, "replay": {
            "path": str(output / "recordings" / "old_camera"), "sessions": ["late"],
            "proxy": {"export": "pinned-generation"}, "speed": 0}, "control": {"enabled": False}})
        src = ReplaySource(cfg.replay, cfg.playback)
        try:
            src.open()
            src.configure()
            consumer = _Consumer()
            src.start(consumer.on_frame)
            _pump(lambda: src.finished)
            assert consumer.ids() == [12, 13, 14, 15]
            assert consumer.stamps() == [T0 + 3_000_000_000 + i * IV for i in range(4)]
            assert all(len(data) == 16 * 16 for _, data, _ in consumer.frames)
            assert src.provenance()["replay_proxies"][0]["export"] == "pinned-generation"
            assert src.playback.descriptor()["proxy"]["lossy"] is True
        finally:
            src.stop()


def test_tiled_bayer_proxy_is_untiled_before_lossy_encoding():
    from cam_driver.bayer_tile import tile_cfa
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        camera = root / "recordings" / "camera"
        camera.mkdir(parents=True)
        pixels = bytes([60, 120, 180, 200] * 64)
        base = _write_session(camera, "bayer", 1, T0, 4,
                              pixels=lambda _: tile_cfa(pixels, 16, 16, "rct", "rggb"))
        header = json.loads(base.with_suffix(".json").read_text())
        header.update(pixel_format="BayerRG8", bayer_pattern="rggb", cfa_tile_mode="rct")
        base.with_suffix(".json").write_text(json.dumps(header))
        output = Path(temporary) / "output"
        output.mkdir()
        req = request(root)
        req["destination"] = str(output)
        convert(req, PROFILES)
        cfg = parse_config({"camera": {"type": "replay"}, "replay": {
            "path": str(output / "recordings" / "camera"), "speed": 0}, "control": {"enabled": False}})
        src = ReplaySource(cfg.replay, cfg.playback)
        try:
            src.open()
            src.configure()
            consumer = _Consumer()
            src.start(consumer.on_frame)
            _pump(lambda: src.finished)
            assert src._untile is None
            assert len(consumer.frames) == 4
            error = sum(abs(a - b) for a, b in zip(consumer.frames[0][1], pixels)) / len(pixels)
            assert error < 10, error
        finally:
            src.stop()


def test_gray16_proxies_match_webrtc_auto_and_reset_between_sessions():
    import numpy as np
    from cam_driver.thermal_preview import PercentileStretch

    for pixel_format, gst_format, dtype in (("GRAY16_LE", "GRAY16_LE", "<u2"),
                                           ("GRAY16_BE", "GRAY16_BE", ">u2"),
                                           ("Mono16", "GRAY16_LE", "<u2")):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            camera = root / "recordings" / "camera"
            camera.mkdir(parents=True)
            expected, ids, timestamps = [], [], []
            for number, (prefix, baseline) in enumerate((("early", 1000), ("late", 50000))):
                # A narrow range within the 16-bit container: top-byte truncation would
                # erase almost all detail. The changing histogram also exercises EMA state.
                pixels = [baseline + np.arange(256, dtype=np.uint16).reshape(16, 16) * 2 + i * 80
                          for i in range(4)]
                first_timestamp = T0 + number * 3_000_000_000
                first_id = 10 + number * 100
                base = _write_session(camera, prefix, number, first_timestamp, len(pixels),
                                      frame_id0=first_id, pixel_format=gst_format,
                                      pixels=lambda i: pixels[i].astype(dtype).tobytes())
                header = json.loads(base.with_suffix(".json").read_text())
                header.update(pixel_format=pixel_format, bits_per_pixel=16)
                base.with_suffix(".json").write_text(json.dumps(header))
                # Each session starts a fresh window; the very different second baseline
                # would render white if normalization state leaked across sessions.
                stretch = PercentileStretch()
                expected.extend(stretch(frame) for frame in pixels)
                ids.extend(first_id + i for i in range(len(pixels)))
                timestamps.extend(first_timestamp + i * IV for i in range(len(pixels)))
            original = {p: file_hash(p) for p in camera.iterdir()}
            output = Path(temporary) / "output"
            output.mkdir()
            req = request(root)
            req["destination"] = str(output)
            result = convert(req, PROFILES)
            assert all(file_hash(p) == digest for p, digest in original.items())
            normalization = req["profile"]["definition"]["gray16_normalization"]
            for session in result["recordings"][0]["sessions"]:
                assert session["pixel_format"] == "GRAY8" and session["bits_per_pixel"] == 8
                assert session["source_pixel_format"] == pixel_format
                assert session["source_bits_per_pixel"] == 16
                assert session["normalization"] == normalization
                assert session["validation"] == {"ok": True, "decoded_frames": 4, "stamps_preserved": True}
                prefix = session["id"]
                path = output / "recordings" / "camera" / prefix
                header = json.loads(path.with_suffix(".json").read_text())
                assert header["pixel_format"] == "GRAY8" and header["bits_per_pixel"] == 8
                assert header["width"] == 16 and header["height"] == 16
                assert header["bayer_pattern"] is None
                proxy = header["proxy"]
                assert proxy["profile_revision"] == 2 and proxy["lossy"] is True
                assert proxy["source_pixel_format"] == pixel_format and proxy["source_bits_per_pixel"] == 16
                assert proxy["normalization"] == normalization
                assert "gray16-percentile-stretch" in proxy["transformations"]
                assert proxy["source_files"] == session["source_files"]
                assert all(file_hash(root / relative) == digest for relative, digest in proxy["source_files"].items())
                with path.with_suffix(".csv").open() as stream, (camera / (prefix + ".csv")).open() as original_csv:
                    assert list(csv.DictReader(stream)) == list(csv.DictReader(original_csv))
            cfg = parse_config({"camera": {"type": "replay"}, "replay": {
                "path": str(output / "recordings" / "camera"), "speed": 0}, "control": {"enabled": False}})
            src = ReplaySource(cfg.replay, cfg.playback)
            try:
                src.open()
                src.configure()
                # GRAY8 is the actual delivered format: ROS publishes mono8 and the
                # WebRTC normalization pump passes it through instead of stretching twice.
                assert src.pixel_format() == "GRAY8"
                consumer = _Consumer()
                src.start(consumer.on_frame)
                _pump(lambda: src.finished)
                assert not src.finished_error
                assert consumer.ids() == ids and consumer.stamps() == timestamps
                assert len(consumer.frames) == len(expected)
                for (_, data, _), normalized in zip(consumer.frames, expected):
                    assert len(data) == 16 * 16
                    actual = np.frombuffer(data, dtype=np.uint8).reshape(16, 16)
                    error = np.abs(actual.astype(np.int16) - normalized.astype(np.int16))
                    assert error.mean() < 8, (pixel_format, error.mean(), error.max())
                for index in (0, 4):
                    frame = np.frombuffer(consumer.frames[index][1], dtype=np.uint8)
                    assert int(frame.max()) - int(frame.min()) > 200
            finally:
                src.stop()


def test_gray16_normalization_honors_video_meta_and_output_padding():
    import gi
    import numpy as np
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
    from cam_driver.export_recordings import _normalize_gray16
    from cam_driver.thermal_preview import PercentileStretch

    width, height, offset, stride = 18, 4, 8, 40
    frame = np.arange(width * height, dtype=np.uint16).reshape(height, width) + 20000
    expected = PercentileStretch()(frame)
    for fmt, dtype in (("GRAY16_LE", "<u2"), ("GRAY16_BE", ">u2")):
        caps = Gst.Caps.from_string(f"video/x-raw,format={fmt},width={width},height={height},framerate=25/1")
        raw = bytearray([255] * (offset + height * stride))
        view = np.ndarray(frame.shape, dtype=dtype, buffer=raw, offset=offset, strides=(stride, 2))
        view[:] = frame

        def sample(data):
            buffer = Gst.Buffer.new_wrapped(bytes(data))
            meta = GstVideo.buffer_add_video_meta_full(
                buffer, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.from_string(fmt),
                width, height, 1, [offset, 0, 0, 0], [stride, 0, 0, 0])
            assert meta is not None
            return Gst.Sample.new(buffer, caps, None, None)

        output, output_caps = _normalize_gray16(sample(raw), PercentileStretch())
        info = GstVideo.VideoInfo.new_from_caps(output_caps)
        assert output_caps.get_structure(0).get_string("format") == "GRAY8"
        assert info.stride[0] == 20 and output.get_size() == info.size
        data = output.extract_dup(0, output.get_size())
        actual = np.ndarray(frame.shape, dtype=np.uint8, buffer=data,
                            offset=info.offset[0], strides=(info.stride[0], 1))
        assert np.array_equal(actual, expected)
        for row in range(height):
            padding = info.offset[0] + row * info.stride[0] + width
            assert data[padding:padding + 2] == b"\0\0"
        short = raw[:offset + (height - 1) * stride + width * 2 - 1]
        raises(lambda: _normalize_gray16(sample(short), PercentileStretch()), "short frame")


def test_missing_video_frames_are_never_published_as_complete():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        camera = root / "recordings" / "camera"
        camera.mkdir(parents=True)
        base = _write_session(camera, "short", 1, T0, 3)
        csvfile = base.with_suffix(".csv")
        csvfile.write_text(csvfile.read_text() + f"3,{3 * IV},{T0 + 3 * IV},system,,{T0 + 3 * IV},{T0 + 3 * IV}\n")
        output = Path(temporary) / "output"
        output.mkdir()
        req = request(root)
        req["destination"] = str(output)
        raises(lambda: convert(req, PROFILES), "CSV has 4 rows but 3 frames decoded")
        assert not (output / "export.yaml").exists()
        assert not (output / "recordings/camera/short.json").exists()


def test_source_pts_misalignment_is_rejected_even_when_counts_match():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "source"
        camera = root / "recordings" / "camera"
        camera.mkdir(parents=True)
        base = _write_session(camera, "wrong-stamp", 1, T0, 3)
        csvfile = base.with_suffix(".csv")
        csvfile.write_text(csvfile.read_text().replace(f"1,{IV},", f"1,{IV + 5_000_000},"))
        output = Path(temporary) / "output"
        output.mkdir()
        req = request(root)
        req["destination"] = str(output)
        raises(lambda: convert(req, PROFILES), "does not align with its CSV PTS")
        assert not (output / "recordings/camera/wrong-stamp.json").exists()


if __name__ == "__main__":
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name, flush=True)
