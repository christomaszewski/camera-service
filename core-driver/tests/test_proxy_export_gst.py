"""Real decode/encode/validate proxy tests. Run inside cam-dev without camera hardware."""
import csv
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cam_driver.config import parse_config
from cam_driver.export_recordings import convert, file_hash
from cam_driver.sources.replay import ReplaySource
from test_proxy_export import request, PROFILES, raises
from test_replay_source import _write_session, T0, IV, _Consumer, _pump


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
