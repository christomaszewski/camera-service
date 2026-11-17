"""Offline camera export contract, validation and exact session binding (stdlib only)."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cam_driver.config import parse_config
from cam_driver.export_recordings import _check_frame_pts, canonical_hash, preflight, safe_relative
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


def test_frame_alignment_keeps_one_fixed_offset_and_one_millisecond_limit():
    def sample(pts):
        return SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=pts))

    points = [12_345_678_900, 12_385_918_900, 12_426_158_900, 16_015_918_900]
    # A large encoder offset is legitimate; Matroska rounds each stored PTS to ms.
    actual = [((pts + 7_000_000_000 + 500_000) // 1_000_000) * 1_000_000 for pts in points]
    offset = None
    for index, (got, expected) in enumerate(zip(actual, points)):
        offset = _check_frame_pts(sample(got), expected, offset, f"frame {index}")
        assert offset == actual[0] - points[0]
    expected = points[-1] + 40_000_000
    assert _check_frame_pts(sample(expected + offset + 1_000_000), expected, offset, "edge") == offset
    raises(lambda: _check_frame_pts(sample(expected + offset + 1_000_001), expected, offset, "edge"),
           "tolerance=1000000 ns")
    raises(lambda: _check_frame_pts(sample(expected + offset - 5_000_000), expected, offset, "boundary"),
           "delta=-5000000 ns")


def test_frame_alignment_rejects_accumulating_drift_and_missing_timestamps():
    def sample(pts):
        return SimpleNamespace(get_buffer=lambda: SimpleNamespace(pts=pts))

    offset = _check_frame_pts(sample(7_000_000_000), 0, None, "frame 0")
    # Each step differs by only 600 us; comparing adjacent frames or resetting
    # offset would accept an arbitrarily drifting recording. One session offset must hold.
    assert _check_frame_pts(sample(7_040_600_000), 40_000_000, offset, "frame 1") == offset
    raises(lambda: _check_frame_pts(sample(7_081_200_000), 80_000_000, offset, "frame 2"),
           "delta=+1200000 ns")
    raises(lambda: _check_frame_pts(sample((1 << 64) - 1), 80_000_000, offset, "frame 2"),
           "decoded frame has no PTS")


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
        session(root, "thermal", pixel_format="BayerRG16")
        session(root, "alpha", pixel_format="RGBA")
        session(root, "odd", width=15)
        req = request(root, ["thermal", "alpha", "odd"])
        for text in ("thermal/capture", "alpha/capture", "odd/capture"):
            raises(lambda: preflight(req, PROFILES), text)


def test_preflight_declares_normalized_mono8_output_and_preserves_source_format():
    for fmt in ("GRAY16_LE", "GRAY16_BE", "Mono16"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session(root, pixel_format=fmt, bits_per_pixel=16)
            item = preflight(request(root), PROFILES)["recordings"][0]["sessions"][0]
            assert item["pixel_format"] == "GRAY8" and item["bits_per_pixel"] == 8
            assert item["source_pixel_format"] == fmt and item["source_bits_per_pixel"] == 16
            assert item["normalization"] == {
                "method": "percentile-stretch", "percentiles": [1.0, 99.0],
                "smoothing": 0.9, "output_pixel_format": "GRAY8", "reset": "session"}
            assert item["lossy"] is True


def test_preflight_rejects_packed_bayer_and_mislabeled_high_depth():
    for fmt, bits, bayer in (("Mono12p", 16, None), ("BayerRG16", 16, "rggb"),
                             ("GRAY16_LE", 16, "rggb"), ("GRAY16_BE", 32, None),
                             ("Mono8", 16, None)):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session(root, pixel_format=fmt, bits_per_pixel=bits, bayer_pattern=bayer)
            raises(lambda: preflight(request(root), PROFILES), "unsupported or incomplete recordings")


def test_preflight_takes_a_16_bit_split_only_where_it_can_be_undone():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        session(root, pixel_format="GRAY16_LE", bits_per_pixel=16, gray16_split_mode="fold")
        item = preflight(request(root), PROFILES)["recordings"][0]["sessions"][0]
        assert item["source_pixel_format"] == "GRAY16_LE" and item["pixel_format"] == "GRAY8"
        assert item["source_gray16_split_mode"] == "fold"      # how the source is stored, per session
    with tempfile.TemporaryDirectory() as temporary:          # a run may hold both storages
        root = Path(temporary)
        session(root, prefix="early", pixel_format="GRAY16_LE", bits_per_pixel=16)
        session(root, prefix="late", pixel_format="GRAY16_LE", bits_per_pixel=16, gray16_split_mode="fold")
        sessions = {s["id"]: s for s in preflight(request(root), PROFILES)["recordings"][0]["sessions"]}
        early, late = sessions["early"], sessions["late"]
        assert "source_gray16_split_mode" not in early and late["source_gray16_split_mode"] == "fold"
        assert {k: v for k, v in late.items() if k not in ("id", "files", "source_gray16_split_mode")} == \
            {k: v for k, v in early.items() if k not in ("id", "files")}
    for header, text in (({"pixel_format": "Mono8", "gray16_split_mode": "fold"}, "a 16-bit split requires a 16-bit recording"),
                         ({"pixel_format": "GRAY16_LE", "bits_per_pixel": 16, "gray16_split_mode": "planes"}, "gray16_split_mode")):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session(root, **header)
            raises(lambda: preflight(request(root), PROFILES), text)


def test_preflight_declares_debayered_color_and_original_pattern():
    for fmt, pattern, declared in (("BayerRG8", "rggb", None), ("BayerGR8", "grbg", None),
                                   ("BayerGB8", "gbrg", None), ("BayerBG8", "bggr", None),
                                   ("GRAY8", "rggb", "rggb"), ("BayerRG8", "bggr", "bggr")):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session(root, pixel_format=fmt, bayer_pattern=declared)
            req = request(root)
            item = preflight(req, PROFILES)["recordings"][0]["sessions"][0]
            assert item["pixel_format"] == "I420" and item["bayer_pattern"] is None
            assert item["bits_per_pixel"] == 8
            assert item["source_pixel_format"] == fmt and item["source_bits_per_pixel"] == 8
            assert item["source_bayer_pattern"] == pattern
            assert item["bayer_conversion"] == req["profile"]["definition"]["bayer_conversion"]


def test_preflight_rejects_invalid_bayer_metadata():
    for fmt, pattern in (("BayerRG8", "invalid"), ("I420", "rggb")):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session(root, pixel_format=fmt, bayer_pattern=pattern)
            raises(lambda: preflight(request(root), PROFILES), "Bayer")


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


def inspect_runtime(image, stdout="", stderr="", returncode=0, architecture="arm64"):
    helper = runpy.run_path(str(ROOT / "tools/export-recordings"))
    proc = subprocess.CompletedProcess([], returncode, stdout, stderr)
    with patch.dict("os.environ", {"CAM_DEV_IMAGE": image}, clear=True), \
            patch("platform.machine", return_value=architecture), \
            patch("subprocess.run", return_value=proc) as run:
        result = helper["runtime"]()
        if architecture in ("arm64", "aarch64", "x86_64", "AMD64"):
            run.assert_called_once_with(["docker", "image", "inspect", image],
                                        capture_output=True, text=True, timeout=15)
        else:
            run.assert_not_called()
        return result


def test_missing_native_digest_reports_exact_pull_requirement_without_pulling():
    image = "registry:5000/cam-dev@sha256:" + "a" * 64
    for architecture, expected in (("arm64", "arm64"), ("x86_64", "amd64")):
        producer, missing, images = inspect_runtime(
            image, stdout="[]", stderr=f"Error response from daemon: No such image: {image}\n",
            returncode=1, architecture=architecture)
        assert producer == {"image": image, "architecture": expected}
        assert len(missing) == 1 and "native image" in missing[0]
        assert images == [{"image": image, "platform": "linux/" + expected}]
    _, missing, images = inspect_runtime(
        image, stderr=f"Error: No such object: {image}\n", returncode=1)
    assert missing and images == [{"image": image, "platform": "linux/arm64"}]


def test_existing_native_image_is_pinned_without_pull_requirement():
    image = "registry:5000/cam-dev:dev"
    image_id = "sha256:" + "b" * 64
    producer, missing, images = inspect_runtime(
        image, stdout=json.dumps([{"Id": image_id, "Architecture": "arm64", "Os": "linux"}]))
    assert producer["image_id"] == image_id
    assert missing == images == []


def test_untagged_missing_image_accepts_implicit_latest_without_changing_pull_ref():
    image = "registry:5000/cam-dev"
    producer, missing, images = inspect_runtime(
        image, stderr=f"Error: No such image: {image}:latest\n", returncode=1)
    assert producer["image"] == image and missing
    assert images == [{"image": image, "platform": "linux/arm64"}]


def test_wrong_architecture_and_unsupported_hosts_are_not_pullable():
    image = "registry:5000/cam-dev:dev"
    _, missing, images = inspect_runtime(
        image, stdout=json.dumps([{"Id": "sha256:existing", "Architecture": "amd64", "Os": "linux"}]))
    assert "need linux/arm64" in missing[0] and images == []
    _, missing, images = inspect_runtime(image, architecture="riscv64")
    assert missing == ["unsupported native architecture riscv64"] and images == []


def test_docker_daemon_permission_and_unrelated_errors_are_not_pullable():
    image = "registry:5000/cam-dev:dev"
    for diagnostic in ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
                       "permission denied while trying to connect to the Docker daemon socket",
                       "Error response from daemon: No such image: some-other-image:dev",
                       f"Error response from daemon: No such image: {image}\nCannot connect to the Docker daemon",
                       f"Cannot connect to Docker socket /tmp/No such image: {image}"):
        _, missing, images = inspect_runtime(image, stderr=diagnostic, returncode=1)
        assert missing[0].startswith("cannot inspect Docker image") and images == []


def test_malformed_image_inspection_is_a_dependency_failure_not_a_pull_requirement():
    image = "registry:5000/cam-dev:dev"
    for output in ("not json", "[]", "{}", "[null]", '["image"]',
                   '[{"Id": "sha256:existing"}]',
                   '[{"Id": null, "Architecture": "arm64", "Os": "linux"}]'):
        _, missing, images = inspect_runtime(image, stdout=output)
        assert missing[0].startswith("Docker image inspection unavailable:") and images == []


def test_helper_preflight_exposes_pull_requirement_without_modifying_run():
    helper = runpy.run_path(str(ROOT / "tools/export-recordings"))
    image = "registry:5000/cam-dev@sha256:" + "c" * 64
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "run"
        session(source)
        original_files = {str(path.relative_to(source)): path.read_bytes()
                          for path in source.rglob("*") if path.is_file()}
        request_path, result_path = root / "request.json", root / "result.json"
        request_path.write_text(json.dumps(request(source)))
        proc = subprocess.CompletedProcess([], 1, "[]", f"Error: No such object: {image}\n")
        with patch.dict("os.environ", {"CAM_DEV_IMAGE": image}, clear=True), \
                patch("platform.machine", return_value="arm64"), \
                patch("subprocess.run", return_value=proc) as run:
            assert helper["main"](["--request", str(request_path), "--result", str(result_path), "--preflight"]) == 0
            run.assert_called_once_with(["docker", "image", "inspect", image],
                                        capture_output=True, text=True, timeout=15)
        result = json.loads(result_path.read_text())
        assert result["missing_images"] == [{"image": image, "platform": "linux/arm64"}]
        assert "native image" in result["missing_dependencies"][0]
        assert result["producer"]["image"] == image
        assert {str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()} == original_files
        assert not (source / "exports").exists()


def _helper_run(headers, probe):
    """tools/export-recordings converting a run whose sessions have `headers`, with Docker stubbed:
    the image exists; `probe` is what asking it about split recordings returns (None: never asked).
    Returns (exit code, the docker commands run, stderr)."""
    import contextlib
    import io
    helper = runpy.run_path(str(ROOT / "tools/export-recordings"))
    image, image_id = "registry:5000/cam-dev:dev", "sha256:" + "d" * 64
    inspect = subprocess.CompletedProcess([], 0, json.dumps([{"Id": image_id, "Architecture": "arm64", "Os": "linux"}]), "")
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return inspect
        if command[:2] == ["docker", "run"] and helper["SPLIT_PROBE"] in command:
            assert probe is not None, "the image was asked about split recordings it was not given"
            return probe
        return subprocess.CompletedProcess(command, 7, "", "")     # the conversion itself: not under test

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source, destination = root / "run", root / "staging"
        destination.mkdir()
        for index, header in enumerate(headers):
            session(source, prefix=f"s{index}", **header)
        req = {**request(source), "destination": str(destination)}
        (root / "request.json").write_text(json.dumps(req))
        stderr = io.StringIO()
        with patch.dict("os.environ", {"CAM_DEV_IMAGE": image}, clear=True), \
                patch("platform.machine", return_value="arm64"), patch("subprocess.run", side_effect=run), \
                contextlib.redirect_stderr(stderr):
            code = helper["main"](["--request", str(root / "request.json"), "--result", str(root / "result.json")])
        assert not any(destination.iterdir()) and not (root / "result.json").exists()
        return code, commands, stderr.getvalue()


_GRAY16 = {"pixel_format": "GRAY16_LE", "bits_per_pixel": 16}
_SPLIT = {**_GRAY16, "gray16_split_mode": "fold"}


def _converted(commands):
    return [c for c in commands if c[:2] == ["docker", "run"] and "cam_driver.export_recordings" in c]


def test_an_image_from_before_split_recordings_is_refused_before_anything_is_converted():
    stale = subprocess.CompletedProcess([], 1, "", "Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\n"
                                        "ImportError: cannot import name 'gray16_split' from 'cam_driver'\n")
    code, commands, err = _helper_run([_GRAY16, _SPLIT], stale)
    assert code == 1 and not _converted(commands)
    assert "registry:5000/cam-dev:dev predates 16-bit split recordings and cannot convert camera/s1" in err
    assert "build or pull a cam-dev image" in err
    probe = commands[1]                                       # asked of the inspected image, sealed like the conversion
    assert probe[:2] == ["docker", "run"] and "sha256:" + "d" * 64 in probe
    assert "--pull=never" in probe and probe[probe.index("--network") + 1] == "none" and "--read-only" in probe


def test_an_image_that_reads_split_recordings_goes_on_to_convert_them():
    ready = subprocess.CompletedProcess([], 0, "", "")
    code, commands, err = _helper_run([_SPLIT], ready)
    assert len(_converted(commands)) == 1                     # (the stub fails the conversion: exit 7)
    assert code == 1 and "offline converter failed (exit 7)" in err and "predates" not in err


def test_an_image_is_only_asked_when_the_request_holds_a_split_session():
    code, commands, _ = _helper_run([_GRAY16, {"pixel_format": "Mono8"}], None)
    assert len(_converted(commands)) == 1 and len(commands) == 3      # inspect, convert, the final rm


def test_a_probe_that_could_not_run_is_not_reported_as_an_old_image():
    broken = subprocess.CompletedProcess([], 125, "", "docker: Error response from daemon: OCI runtime create failed\n")
    code, commands, err = _helper_run([_SPLIT], broken)
    assert code == 1 and not _converted(commands)
    assert "cannot run registry:5000/cam-dev:dev to check it reads 16-bit split recordings" in err
    assert "OCI runtime create failed" in err and "predates" not in err


def test_the_implementation_identity_covers_the_split_join():
    source = (ROOT / "tools/export-recordings").read_text()
    assert '"core-driver/cam_driver/gray16_split.py"' in source


if __name__ == "__main__":
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
