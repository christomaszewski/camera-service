"""Exercise the hardware qualification's verdict without an NVIDIA device.

Only gst-launch is replaced. The real validator writes/compares bytes and returns
its process verdict, including truncation/extra-data cases that used to pass.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("damage", ["none", "missing", "empty", "partial", "extra", "trailing", "pixel", "encode_error", "decode_error"])
def test_nvenc_verdict_requires_every_byte_and_frame(tmp_path, monkeypatch, damage):
    path = Path(__file__).resolve().parents[1] / "tools/nvenc_lossless_test.py"
    spec = importlib.util.spec_from_file_location("nvenc_validation", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.W, tool.H, tool.IMG = 8, 8, 64
    tool.IN, tool.ENC, tool.OUT = [str(tmp_path / n) for n in ("in.raw", "enc.mkv", "out.raw")]
    monkeypatch.setattr(tool.sys, "argv", ["nvenc_lossless_test", "--frames", "3", "--feed", "videoconvert"])
    calls = []

    def launch(argv, **kwargs):
        stage = "decode" if "avdec_h265" in argv else "encode"
        calls.append(stage)
        if damage == stage + "_error":
            return SimpleNamespace(returncode=1, stdout="injected pipeline failure")
        if stage == "encode":
            Path(tool.ENC).write_bytes(b"simulated encoded data")
        else:
            data = Path(tool.IN).read_bytes()
            outputs = {"missing": data[:-64], "empty": b"", "partial": data[:-1],
                       "extra": data + data[:64], "trailing": data + b"x",
                       "pixel": bytes([data[0] ^ 1]) + data[1:]}
            Path(tool.OUT).write_bytes(outputs.get(damage, data))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(tool.subprocess, "run", launch)
    assert tool.main() == (0 if damage == "none" else 1)
    assert calls == (["encode"] if damage == "encode_error" else ["encode", "decode"])


def _tool(tmp_path, monkeypatch, argv, exact, width=8, height=8):
    """The validator with gst-launch replaced: `exact` says which feeds decode back unchanged."""
    path = Path(__file__).resolve().parents[1] / "tools/nvenc_lossless_test.py"
    spec = importlib.util.spec_from_file_location("nvenc_validation", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    tool.W, tool.H, tool.IMG = width, height, width * height
    tool.IN, tool.ENC, tool.OUT = [str(tmp_path / n) for n in ("in.raw", "enc.mkv", "out.raw")]
    monkeypatch.setattr(tool.sys, "argv", ["nvenc_lossless_test", "--frames", "2"] + argv)
    feeds = []

    def launch(argv, **kwargs):
        if "avdec_h265" not in argv:
            feeds.append("nv12" if "format=nv12" in argv else "videoconvert")
            source = Path(next(a for a in argv if a.startswith("location=") and a.endswith((".raw", ".nv12")))[9:])
            # each feed's encode reads its own file: the plane alone, or the plane and its chroma
            frame = tool.IMG + (tool.IMG // 2 if feeds[-1] == "nv12" else 0)
            assert source.stat().st_size == 2 * frame
            if feeds[-1] == "nv12":
                data = source.read_bytes()
                assert all(data[i * frame + tool.IMG:(i + 1) * frame] == bytes([128]) * (tool.IMG // 2) for i in range(2))
                assert b"".join(data[i * frame:i * frame + tool.IMG] for i in range(2)) == Path(tool.IN).read_bytes()
            Path(tool.ENC).write_bytes(b"simulated")
        else:
            data = Path(tool.IN).read_bytes()
            Path(tool.OUT).write_bytes(data if feeds[-1] in exact else bytes([data[0] ^ 1]) + data[1:])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(tool.subprocess, "run", launch)
    return tool, feeds


@pytest.mark.parametrize("argv, exact, verdict, ran", [
    ([], {"videoconvert", "nv12"}, 0, ["videoconvert", "nv12"]),
    # the service proves nv12 itself and falls back: nv12 failing alone is reported, not fatal
    ([], {"videoconvert"}, 0, ["videoconvert", "nv12"]),
    # ... but there is nothing exact behind videoconvert
    ([], {"nv12"}, 1, ["videoconvert", "nv12"]),
    ([], set(), 1, ["videoconvert", "nv12"]),
    # asked for by name, a feed decides the verdict
    (["--feed", "nv12"], {"videoconvert"}, 1, ["nv12"]),
    (["--feed", "nv12"], {"nv12"}, 0, ["nv12"]),
    (["--feed", "videoconvert"], {"nv12"}, 1, ["videoconvert"]),
])
def test_both_feeds_are_qualified_and_videoconvert_decides(tmp_path, monkeypatch, capsys, argv, exact, verdict, ran):
    tool, feeds = _tool(tmp_path, monkeypatch, argv, exact)
    assert tool.main() == verdict
    assert feeds == ran
    out = capsys.readouterr().out
    assert ("will keep it on videoconvert" in out) == (not argv and exact == {"videoconvert"})


def test_geometry_the_nv12_feed_cannot_carry_is_skipped_or_refused(tmp_path, monkeypatch, capsys):
    tool, feeds = _tool(tmp_path, monkeypatch, [], {"videoconvert"}, width=6)
    assert tool.main() == 0 and feeds == ["videoconvert"]
    assert "nv12 feed skipped" in capsys.readouterr().out
    tool, feeds = _tool(tmp_path, monkeypatch, ["--feed", "nv12"], set(), height=7)
    with pytest.raises(SystemExit):
        tool.main()
    assert feeds == []


def test_the_tool_encodes_what_the_recorder_encodes():
    """The Jetson promotion gate must qualify the pipeline the vehicle runs: the tool mirrors the
    recorder's fragment by hand (it runs standalone), so hold the two together here."""
    import cam_driver.recorder as rec
    from cam_driver.config import RecordingConfig
    from test_recorder import _FakeGst, _JETSON
    path = Path(__file__).resolve().parents[1] / "tools/nvenc_lossless_test.py"
    spec = importlib.util.spec_from_file_location("nvenc_validation", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.LUMA_COLORIMETRY == rec.LUMA_COLORIMETRY
    assert tool.HW_HEVC_LOSSLESS == rec._hw_hevc_lossless(RecordingConfig(), 0, 0).split()
    real = rec.Gst
    rec.Gst = _FakeGst(_JETSON)
    try:
        for feed in tool.FEEDS:
            desc, enc = rec.build_recorder_description(RecordingConfig(), 8, "/x", 25.0, False, None, luma_feed=feed)
            assert enc == "hw-hevc-lossless"
            recorder = desc.split()
            recorder = recorder[recorder.index("!") + 1:recorder.index("splitmuxsink") - 1]   # after the queue
            argv = tool.encode_argv(feed)
            mine = argv[argv.index("framerate=25/1") + 2:argv.index("matroskamux") - 1]      # after rawvideoparse
            if feed == "nv12":
                # the recorder's appsrc carries the caps the tool has to assert on a raw file
                assert mine[:3] == ["capssetter", f"caps=video/x-raw,colorimetry=(string){rec.LUMA_COLORIMETRY}", "!"]
                assert f"colorimetry={rec.LUMA_COLORIMETRY}" in rec.nv12_feed_caps(8, 8, "25/1")
                mine = mine[3:]
            assert mine == recorder, (feed, mine, recorder)
    finally:
        rec.Gst = real
