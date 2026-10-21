"""Offline camera export contract, validation and exact session binding (stdlib only)."""
import json
from pathlib import Path
import runpy
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cam_driver.config import parse_config
from cam_driver.export_recordings import canonical_hash, preflight, safe_relative
from cam_driver.playback import discover_sessions

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "export-profiles"


def request(root, names=("camera",)):
    definition = json.loads((PROFILES / "h264.yaml").read_text())
    return {"version": 1, "source_root": str(root), "destination": None,
            "profile": {"id": "h264", "sha256": canonical_hash(definition), "definition": definition},
            "recordings": [{"name": n, "relative": "recordings/" + n, "sessions": []} for n in names]}


def session(root, name="camera", prefix="capture", **header):
    directory = root / "recordings" / name
    directory.mkdir(parents=True, exist_ok=True)
    hdr = {"pixel_format": "Mono8", "width": 16, "height": 16,
           "first_timestamp_ns": 1_000_000_000, "cfa_tile_mode": "off", **header}
    (directory / (prefix + ".json")).write_text(json.dumps(hdr))
    (directory / (prefix + ".csv")).write_text("frame_id,pts_ns,timestamp_ns,source,chunk_ns,camera_ns,system_ns\n"
                                               "5,0,1000000000,system,,1000000000,1000000000\n")
    (directory / (prefix + "-00000.mkv")).write_bytes(b"placeholder media for host preflight")
    return directory


def raises(fn, text):
    try:
        fn()
    except ValueError as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError("expected error: " + text)


def test_preflight_uses_portable_paths_and_keeps_unknown_recorded_names():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        session(root, "old_camera_name")
        result = preflight(request(root, ["old_camera_name"]), PROFILES)
        item = result["recordings"][0]["sessions"][0]
        assert item["frame_count"] == 1 and item["start_ns"] == 1_000_000_000
        assert item["lossy"] is True and item["format"] == "camera-recording-proxy/v1"
        assert item["files"] == ["recordings/old_camera_name/capture.json", "recordings/old_camera_name/capture.csv",
                                 "recordings/old_camera_name/capture-00000.mkv"]
        assert not (root / "exports").exists()


def test_preflight_rejects_all_unsupported_inputs_together():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        session(root, "thermal", pixel_format="Mono16")
        session(root, "alpha", pixel_format="RGBA")
        session(root, "odd", width=15)
        req = request(root, ["thermal", "alpha", "odd"])
        for text in ("thermal/capture", "alpha/capture", "odd/capture"):
            raises(lambda: preflight(req, PROFILES), text)


def test_profile_hash_and_capture_timeline_validation():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = session(root)
        req = request(root)
        req["profile"]["definition"]["encoding"]["crf"] = 5
        raises(lambda: preflight(req, PROFILES), "profile definition/hash differs")
        req = request(root)
        csv = directory / "capture.csv"
        csv.write_text(csv.read_text() + "6,0,1000000001,system,,1000000001,1000000001\n")
        raises(lambda: preflight(req, PROFILES), "strictly increasing")


def test_preflight_cannot_follow_recording_symlinks_outside_run():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "run"
        root.mkdir()
        directory = session(root)
        other = Path(temporary) / "outside.mkv"
        other.write_bytes(b"other run")
        target = directory / "capture-00000.mkv"
        target.unlink()
        target.symlink_to(other)
        raises(lambda: preflight(request(root), PROFILES), "escapes its root")
        raises(lambda: safe_relative(root, "../outside.mkv"), "unsafe recording path")


def test_exact_session_list_is_complete_ordered_and_mutually_exclusive():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        directory = session(root, prefix="early", first_timestamp_ns=1)
        session(root, prefix="late", first_timestamp_ns=3)
        session(root, prefix="middle", first_timestamp_ns=2)
        selected = discover_sessions(str(directory), sessions=["late", "early"])
        assert [Path(run.base).name for run in selected] == ["early", "late"]
        raises(lambda: discover_sessions(str(directory), sessions=["missing"]), "not found")
        raises(lambda: discover_sessions(str(directory), "early", ["late"]), "mutually exclusive")
        for value in ([], ["early", "early"], ["../early"], "early"):
            raises(lambda: parse_config({"replay": {"sessions": value}}), "replay.sessions")
        raises(lambda: parse_config({"replay": {"run": "early", "sessions": ["late"]}}), "mutually exclusive")
        assert parse_config({"replay": {"sessions": ["early"]}}).replay.sessions == ["early"]


def test_export_helper_selects_native_dev_image_and_honors_explicit_pins():
    helper = runpy.run_path(str(ROOT / "tools/export-recordings"))
    image = helper["image_reference"]
    assert image({}) == "cam-dev:dev"
    assert image({"RIG_IMAGE_REGISTRY": "registry:5000", "RIG_IMAGE_TAG": "v1.2"}) == "registry:5000/cam-dev:v1.2-dev"
    assert image({"RIG_IMAGE_TAG": "v1.2-jp6m"}) == "cam-dev:v1.2-dev"
    assert image({"CAM_IMAGE_TAG": "special"}) == "cam-dev:special"
    assert image({"CAM_DEV_IMAGE": "my-image@sha256:abc"}) == "my-image@sha256:abc"


if __name__ == "__main__":
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
