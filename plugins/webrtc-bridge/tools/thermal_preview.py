"""Compatibility import for the shared camera thermal-preview transform.

The image bundles the canonical module beside this shim. A checkout imports it
from core-driver so WebRTC and offline proxy exports always share one algorithm.
"""
try:
    from _thermal_preview import DEFAULT_WINDOW, PercentileStretch, parse_window
except ModuleNotFoundError as exc:
    if exc.name != "_thermal_preview":
        raise
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "core-driver"))
    from cam_driver.thermal_preview import DEFAULT_WINDOW, PercentileStretch, parse_window

__all__ = ["DEFAULT_WINDOW", "PercentileStretch", "parse_window"]
