"""Capture sources: the frontend seam (device + timestamp policy + feeder).

The pipeline downstream of the appsrc is source-agnostic; a Source produces
(FrameStamp, image_bytes) per frame. See base.Source and factory.make_source.
"""
from .base import OnFrame, Source
from .factory import make_source

__all__ = ["Source", "OnFrame", "make_source"]
