"""Source factory: pick the capture-source implementation from config (default gige)."""
from __future__ import annotations

from .base import Source


def make_source(cfg) -> Source:
    """Instantiate the configured source. Implementations are imported lazily so this
    module (and the unit tests) don't require a source's native deps (e.g. Aravis)."""
    stype = (cfg.source.type or "gige").strip().lower()
    if stype == "gige":
        from .gige import GigeSource
        return GigeSource(cfg.camera)
    raise ValueError(f"unknown source type {cfg.source.type!r} (known: gige)")
