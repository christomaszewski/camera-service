"""Tests for tools/sensor_env.py -- the config -> compose-env derivation cam-up evals.

Covers the PLAYBACK INPUT BIND specifically: which host path gets mounted where, for a pcap or
replay source. That derivation is easy to get quietly wrong (a bad bind is a container that
starts fine and then can't find its data), and one shape -- a path already inside the data
root -- must emit NOTHING, because compose merges volumes by target and a second read-only
bind there would replace the recordings mount.

And the BAYER LABEL under playback: the bridges' CAM_BAYER / CAM_ROS_ENCODING hint is derived
from the camera pixel_format, and a `rig replay` config names the source `replay` -- a block with
no pixel_format -- while the camera's own block is still in the file. It must inherit, and only
a playback source may.

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


# ---- the Bayer label under playback (the rig-replay label regression) -------------------------

def _bridged(cfg):
    """Both bridges on, as a sensor deployment runs them: the label env is only derived for a
    config that runs a bridge."""
    cfg["plugins"] = [{"name": "ros2-bridge", "enabled": True, "isolation": "container",
                       "params": {"topic": "image_raw"}},
                      {"name": "webrtc-bridge", "enabled": True, "isolation": "container",
                       "params": {"port": 8443}}]
    return cfg


def _replay_of(live_block, live, path="/data/recordings/runs/7/recordings/cam"):
    """A rig-rendered replay config: the instance's own yaml (its live block intact) with
    `camera.type: replay` + a replay: block patched on top -- what `rig replay`'s overrides
    (rigging.yaml `replay.source`) produce."""
    return _bridged({"name": "cam", "camera": {"type": "replay"}, live_block: live,
                     "replay": {"path": path, "retime": "original", "loop": False}})


def test_replay_inherits_the_bayer_label_from_the_live_block():
    # THE REPLAY-LABEL REGRESSION. rig replay patches camera.type to `replay`; the replay: block
    # names no pixel_format (the recording's sidecar carries it), and reading only the active
    # block dropped CAM_BAYER / CAM_ROS_ENCODING to '' -- a color camera replayed as a gray
    # mosaic on the JP6/dev header transport, its topic mono8 instead of bayer_rggb8.
    e = _env(_replay_of("gige", {"pixel_format": "BayerRG8", "packet_size": 9000}))
    assert e["CAM_SOURCE_TYPE"] == "replay"            # still a playback source (the REPLAY chip)
    assert e["CAM_ROS_ENCODING"] == "bayer_rggb8"
    assert e["CAM_BAYER"] == "rggb"


def test_replay_of_a_16bit_thermal_stays_on_the_mono_path():
    e = _env(_replay_of("usb", {"pixel_format": "GRAY16_LE", "width": 640, "height": 512}))
    assert e["CAM_ROS_ENCODING"] == "" and e["CAM_BAYER"] == ""   # mono16 comes off the header


def test_replay_of_a_capture_fed_instance_inherits_the_pcap_pin():
    cfg = _replay_of("pcap", {"path": "/x/thermal.pcapng", "pixel_format": "GRAY16_LE"})
    e = _env(cfg)
    assert e["CAM_ROS_ENCODING"] == "" and e["CAM_BAYER"] == ""
    cfg["pcap"]["pixel_format"] = "BayerGR8"           # not a UVC shape, but the rule is uniform
    assert _env(cfg)["CAM_BAYER"] == "grbg"


def test_replay_with_no_live_block_stays_mono():
    e = _env(_bridged(_replay("/mnt/archive/run-2026-08-01")))
    assert e["CAM_ROS_ENCODING"] == "" and e["CAM_BAYER"] == ""


def test_a_live_source_never_borrows_another_blocks_format():
    # An rtsp camera has no pixel_format by design (the format comes off the decoded stream); a
    # stale gige: block left in the file must not relabel it.
    e = _env(_bridged({"name": "cam", "camera": {"type": "rtsp"}, "rtsp": {"url": "rtsp://x/y"},
                       "gige": {"pixel_format": "BayerRG8"}}))
    assert e["CAM_ROS_ENCODING"] == "" and e["CAM_BAYER"] == ""


def test_the_active_playback_blocks_format_wins_over_a_stale_one():
    e = _env(_bridged({"name": "cam", "camera": {"type": "pcap"},
                       "pcap": {"path": "t.pcapng", "pixel_format": "GRAY8"},
                       "gige": {"pixel_format": "BayerRG8"}}))
    assert e["CAM_ROS_ENCODING"] == "" and e["CAM_BAYER"] == ""


def test_disagreeing_source_blocks_take_the_first_and_say_so():
    cfg = _replay_of("gige", {"pixel_format": "BayerRG8"})
    cfg["usb"] = {"pixel_format": "GRAY16_LE"}
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        e = _env(cfg)
    assert e["CAM_BAYER"] == "rggb"
    assert "disagree" in err.getvalue() and "usb: GRAY16_LE" in err.getvalue()


def test_the_stdlib_parser_sees_the_same_file_as_pyyaml():
    # A vehicle host without PyYAML runs the fallback parser, on the file rig RENDERED -- which
    # PyYAML dumped, so its plugins list items sit at column 0 ("- name:" flush with `plugins:`),
    # not indented as a hand-written config has them. The fallback must read that shape (it
    # used to drop the whole plugins list -> no compose profiles -> no bridges), the playback
    # block (the input bind -- it used to skip pcap/replay, binding an absolute replay path as
    # the bare-name input folder) AND the live block (the inherited label): one env from one
    # file, whichever parser runs. yaml.safe_dump in _env is exactly rig's dump shape.
    cfg = _replay_of("gige", {"pixel_format": "BayerRG8"}, path="/mnt/archive/run-2026-08-01")
    with_yaml = _env(cfg)
    sensor_env._HAVE_YAML = False
    try:
        without = _env(cfg)
    finally:
        sensor_env._HAVE_YAML = True
    for k in ("COMPOSE_PROFILES", "CAM_SOURCE_TYPE", "CAM_INPUT_SRC", "CAM_INPUT_DST",
              "CAM_ROS_TOPIC", "CAM_ROS_ENCODING", "CAM_BAYER", "CAM_SIGNALLING_PORT"):
        assert without.get(k) == with_yaml.get(k), (k, without.get(k), with_yaml.get(k))
    assert without["COMPOSE_PROFILES"] == "ros2-bridge,webrtc-bridge"
    assert without["CAM_BAYER"] == "rggb"
    assert without["CAM_INPUT_SRC"] == "/mnt/archive/run-2026-08-01"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
