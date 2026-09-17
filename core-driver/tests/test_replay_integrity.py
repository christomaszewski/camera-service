"""Damaged archives must never finish as a faithful replay. Real FFV1 readers."""
import json
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from cam_driver.playback import discover_sessions
from test_replay_source import _write_session, _source, _pump, T0


@pytest.mark.parametrize("change", ["csv_short", "csv_long", "video_truncated", "video_empty"])
def test_incomplete_video_or_sidecar_is_a_replay_error(tmp_path, change):
    base = _write_session(tmp_path, "cam", 1, T0, 20)
    csv = base.with_suffix(".csv")
    mkv = Path(str(base) + "-00000.mkv")
    if change.startswith("csv_"):
        rows = csv.read_text().splitlines()
        rows = rows[:-1] if change == "csv_short" else rows + [rows[-1]]
        csv.write_text("\n".join(rows) + "\n")
    else:
        data = mkv.read_bytes()
        mkv.write_bytes(data[:len(data) // 2] if change == "video_truncated" else b"")
    try:
        src = _source(tmp_path, loop=True)
    except ValueError as exc:
        assert "video" in str(exc) or "record" in str(exc)
        return
    frames = []
    try:
        src.start(lambda st, data: frames.append(st))
        _pump(lambda: src.finished, timeout_s=5)
        assert src.finished_error, "incomplete replay must not finish successfully or loop"
        assert src._cycle == 0
        if change == "csv_short":
            assert len(frames) == 19, "do not invent timestamps for an unindexed video frame"
    finally:
        src.stop()


@pytest.mark.parametrize("removed", [0, 1, 2])
def test_missing_segment_is_rejected_before_any_frames(tmp_path, removed):
    base = _write_session(tmp_path, "cam", 1, T0, 3)
    first = Path(str(base) + "-00000.mkv")
    # Discovery is container-independent: represent three attested segments, then remove one.
    for i in (1, 2):
        Path(f"{base}-{i:05d}.mkv").write_bytes(first.read_bytes())
    header = json.loads(base.with_suffix(".json").read_text())
    header["session"] = {"segments": 3, "frames_recorded": 9, "truncated": False, "error": None}
    base.with_suffix(".json").write_text(json.dumps(header))
    Path(f"{base}-{removed:05d}.mkv").unlink()
    with pytest.raises(ValueError, match="segment"):
        discover_sessions(str(tmp_path))


def test_legacy_summary_undercount_does_not_reject_complete_video(tmp_path):
    base = _write_session(tmp_path, "cam", 1, T0, 3)
    header = json.loads(base.with_suffix(".json").read_text())
    header["session"] = {"segments": 0, "frames_recorded": 3, "truncated": False, "error": None}
    base.with_suffix(".json").write_text(json.dumps(header))
    src = _source(tmp_path)
    frames = []
    try:
        src.start(lambda st, data: frames.append(st.frame_id))
        _pump(lambda: src.finished)
        assert frames == [0, 1, 2] and not src.finished_error
    finally:
        src.stop()


@pytest.mark.parametrize("attestation", [
    {"sidecar_csv_failed": True},
    {"session": {"frames_recorded": 3, "truncated": True}},
    {"session": {"frames_recorded": 3, "truncated": False, "error": "write failed"}},
])
def test_known_incomplete_recording_is_not_silently_replayed(tmp_path, attestation):
    base = _write_session(tmp_path, "cam", 1, T0, 3)
    header = json.loads(base.with_suffix(".json").read_text())
    base.with_suffix(".json").write_text(json.dumps({**header, **attestation}))
    with pytest.raises(ValueError, match="incomplete"):
        _source(tmp_path)


def test_service_exits_nonzero_when_video_and_csv_disagree(tmp_path):
    run = tmp_path / "input"
    run.mkdir()
    base = _write_session(run, "cam", 1, T0, 5)
    csv = base.with_suffix(".csv")
    rows = csv.read_text().splitlines()
    csv.write_text("\n".join(rows + [rows[-1]]) + "\n")
    config = tmp_path / "replay.json"
    config.write_text(json.dumps({
        "camera": {"type": "replay"}, "replay": {"path": str(run), "speed": 0},
        "playback": {"on_finish": "exit"}, "recording": {"enabled": False},
        "preview": {"enabled": False}, "control": {"enabled": False},
        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}},
    }))
    main = Path(__file__).resolve().parents[1] / "main.py"
    result = subprocess.run([sys.executable, str(main), "-c", str(config)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "NOT trustworthy" in result.stderr, result.stderr
