"""Tests for tools/sensor_env.py -- the config -> compose-env derivation cam-up evals.

Covers the PLAYBACK INPUT BIND specifically: which host path gets mounted where, for a pcap or
replay source. That derivation is easy to get quietly wrong (a bad bind is a container that
starts fine and then can't find its data), and one shape -- a path already inside the data
root -- must emit NOTHING, because compose merges volumes by target and a second read-only
bind there would replace the recordings mount.

Run: python3 core-driver/tests/test_sensor_env.py
"""
import contextlib
import io
import os
import shlex
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import sensor_env  # noqa: E402


def _env(cfg, **environ):
    """Derive the compose env for one config, exactly as cam-up does.

    Drives the real CLI contract -- a YAML file in, shell-quoted KEY=value lines out -- rather than
    an internal function, because those lines are what cam-up `eval`s. Parsed back with shlex so
    the quoting is exercised too."""
    saved = {k: os.environ.get(k) for k in ("CAM_INPUT_DIR", "RIG_DATA_DIR", "COMPOSE_PROJECT_NAME")}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    try:
        yaml.safe_dump(cfg, tmp)
        tmp.close()
        for k in saved:
            os.environ.pop(k, None)
        for k, v in environ.items():
            os.environ[k] = v
        out = io.StringIO()
        argv = sys.argv
        try:
            sys.argv = ["sensor_env.py", tmp.name]
            with contextlib.redirect_stdout(out):
                rc = sensor_env.main()
        finally:
            sys.argv = argv
        assert rc == 0, f"sensor_env exited {rc}"
        env = {}
        for line in out.getvalue().splitlines():
            k, _, v = line.partition("=")
            parts = shlex.split(v)
            env[k] = parts[0] if parts else ""
        return env
    finally:
        os.unlink(tmp.name)
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _pcap(path):
    return {"name": "cam", "camera": {"type": "pcap"}, "pcap": {"path": path}}


def _replay(path):
    return {"name": "cam", "camera": {"type": "replay"}, "replay": {"path": path}}


def test_bare_pcap_name_mounts_the_input_folder():
    e = _env(_pcap("thermal.pcapng"))
    assert e["CAM_INPUT_SRC"] == "./input"      # dev default: the dir already in the repo
    assert e["CAM_INPUT_DST"] == "/input"       # the long-documented container mount point
    assert e["CAM_INPUT_ROOT"] == "/input"      # -> the core joins the bare name onto this


def test_bare_pcap_name_honors_an_absolute_input_dir_and_self_maps_it():
    e = _env(_pcap("thermal.pcapng"), CAM_INPUT_DIR="/data/captures")
    # Absolute input dir -> host and container agree, so `rig bake` keeps it literal.
    assert e["CAM_INPUT_SRC"] == e["CAM_INPUT_DST"] == "/data/captures"
    assert e["CAM_INPUT_ROOT"] == "/data/captures"


def test_relative_input_dir_still_lands_on_an_absolute_container_path():
    # A relative CAM_INPUT_DIR is a fine HOST side, but the container target must be absolute.
    e = _env(_pcap("thermal.pcapng"), CAM_INPUT_DIR="captures")
    assert e["CAM_INPUT_SRC"] == "captures"
    assert e["CAM_INPUT_DST"] == "/input"


def test_absolute_pcap_path_self_maps_and_exports_no_root():
    e = _env(_pcap("/data/captures/thermal.pcapng"))
    assert e["CAM_INPUT_SRC"] == e["CAM_INPUT_DST"] == "/data/captures/thermal.pcapng"
    # No root: an absolute path pins itself, so the container's CAM_INPUT_DIR stays empty rather
    # than advertising a "folder" that is really a file.
    assert "CAM_INPUT_ROOT" not in e


def test_replay_inside_the_data_root_emits_no_bind():
    # THE REGRESSION THIS FILE EXISTS FOR. `replay.path: /data/recordings` is the documented shape
    # and already sits in the base compose's read-write recordings bind. Compose merges volumes by
    # TARGET, so emitting a second read-only bind on /data/recordings would REPLACE that mount --
    # flipping recordings read-only (a re-recording replay then fails) and swapping its host side.
    for path in ("/data/recordings", "/data/recordings/runs/7"):
        e = _env(_replay(path))
        assert "CAM_INPUT_SRC" not in e, f"{path} must ride the existing data-root mount"


def test_replay_inside_a_rig_data_root_emits_no_bind():
    e = _env(_replay("/mnt/data/runs/7"), RIG_DATA_DIR="/mnt/data")
    assert "CAM_INPUT_SRC" not in e


def test_replay_outside_the_data_root_gets_its_own_bind():
    e = _env(_replay("/mnt/archive/run-2026-08-01"))
    assert e["CAM_INPUT_SRC"] == e["CAM_INPUT_DST"] == "/mnt/archive/run-2026-08-01"


def test_a_sibling_of_the_data_root_is_not_mistaken_for_being_inside_it():
    # /data/recordings-old must NOT match the /data/recordings prefix.
    e = _env(_replay("/data/recordings-old/run-3"))
    assert e["CAM_INPUT_SRC"] == "/data/recordings-old/run-3"


def test_live_sources_get_no_input_bind():
    for cfg in ({"name": "c", "camera": {"type": "gige"}, "gige": {"fake": True}},
                {"name": "c", "camera": {"type": "rtsp"}, "rtsp": {"uri": "rtsp://x/y"}}):
        e = _env(cfg)
        assert "CAM_INPUT_SRC" not in e and "CAM_INPUT_ROOT" not in e


def test_a_yaml_param_cannot_forge_an_input_bind():
    # The UPPERCASE plugin-param passthrough must never be able to mount an arbitrary host path.
    cfg = _pcap("t.pcapng")
    cfg["plugins"] = [{"name": "webrtc-bridge", "enabled": True, "isolation": "container",
                       "params": {"CAM_INPUT_SRC": "/etc", "CAM_INPUT_DST": "/etc",
                                  "CAM_INPUT_ROOT": "/etc", "CAM_INPUT_DIR": "/etc"}}]
    e = _env(cfg)
    assert e["CAM_INPUT_SRC"] == "./input" and e["CAM_INPUT_DST"] == "/input"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
