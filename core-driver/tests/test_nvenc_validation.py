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
    monkeypatch.setattr(tool.sys, "argv", ["nvenc_lossless_test", "--frames", "3"])
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
