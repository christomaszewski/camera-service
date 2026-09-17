"""Runtime recording settings and session snapshots (no GStreamer or Zenoh dependency)."""
from copy import deepcopy
from dataclasses import fields
import json
import math
import os

from .config import RecordingConfig
from .formats import VALID_ENCODERS

PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
           "slow", "slower", "veryslow", "placebo")
CHOICES = {
    "encoder": VALID_ENCODERS,
    "x264_preset": PRESETS,
    "bayer_tile": ("off", "plain", "green_diff", "rct"),
    "nvenc_preset": ("", "disable", "ultrafast", "fast", "medium", "slow"),
}
BOUNDS = {"x264_crf": (1, 50, int), "segment_seconds": (1, 86400, int),
          "keyframe_interval_s": (0, 3600, float), "bframes": (0, 16, int),
          "videoconvert_threads": (0, 64, int)}
EDITABLE = tuple(CHOICES) + tuple(BOUNDS) + ("nvenc_maxperf",)


def requested(cfg):
    """Include deployment-only fields in the audit, but never accept them in a remote patch."""
    defaults = RecordingConfig()
    return {f.name: deepcopy(getattr(cfg, f.name, getattr(defaults, f.name)))
            for f in fields(RecordingConfig)}


def apply_patch(cfg, patch):
    if not isinstance(patch, dict) or not patch:
        raise ValueError("settings must be a non-empty JSON object")
    unknown = set(patch) - set(EDITABLE)
    if unknown:
        raise ValueError("settings are not editable: " + ", ".join(sorted(unknown)))
    for key, value in patch.items():
        if key in CHOICES:
            if not isinstance(value, str) or value not in CHOICES[key]:
                raise ValueError(f"{key} must be one of {CHOICES[key]}")
        elif key in BOUNDS:
            lo, hi, kind = BOUNDS[key]
            types = (int,) if kind is int else (int, float)
            if (isinstance(value, bool) or not isinstance(value, types)
                    or not math.isfinite(value) or not lo <= value <= hi):
                raise ValueError(f"{key} must be {'an integer' if kind is int else 'a number'} from {lo} to {hi}")
        elif type(value) is not bool:
            raise ValueError(f"{key} must be a boolean")
    candidate = RecordingConfig(**{**requested(cfg), **patch})
    return candidate


def write_snapshot(path, snapshot):
    """Flush and publish before feeding the recorder; a failed audit write refuses activation."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "x") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
