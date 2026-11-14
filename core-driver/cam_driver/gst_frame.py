"""A frame carried in GStreamer memory instead of materializing Python bytes.

Every outgoing buffer has its own metadata and shares only the immutable pixel memory. Holding
the source buffer keeps that memory alive across decoder pool reuse, pause, and reader teardown.
Live sources detach from capture pools with owned_copy(); replay can retain decoder memory.
Pixel transforms can explicitly request bytes when needed.
"""
from __future__ import annotations

import os
from typing import Union

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst


class GstFrame:
    def __init__(self, buffer):
        self._buffer = buffer
        self._size = buffer.get_size()

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        return self._buffer.extract_dup(0, self._size)

    @classmethod
    def owned_copy(cls, buffer, offset=0, size=None):
        """Release a live capture pool promptly, sharing only our own pixel allocation.

        A recorder can retain more frames than a V4L2/decoder/shm pool contains. Copy
        pixels in GStreamer without carrying parent-buffer metadata or Python bytes;
        downstream outputs then share this allocation instead of copying it again.
        """
        size = buffer.get_size() - offset if size is None else size
        owned = buffer.copy_region(Gst.BufferCopyFlags.MEMORY | Gst.BufferCopyFlags.DEEP,
                                   offset, size)
        if owned is None:
            raise RuntimeError("could not copy live frame out of its capture pool")
        return cls(owned)

    def to_buffer(self, pts: int, frame_id: int, prefix: bytes = b""):
        # MEMORY alone deliberately excludes decoder PTS, duration, flags, and metadata: the
        # service restamps frames exactly as its byte-based appsrc path does. copy() is not
        # sufficient in PyGI, where boxed copies can just ref the SAME mutable GstBuffer.
        buf = self._buffer.copy_region(Gst.BufferCopyFlags.MEMORY, 0, self._size)
        if prefix:
            # Only the small CAMF header is wrapped through Python. shmsink handles copying
            # the resulting memory blocks into shared memory without a Python image roundtrip.
            buf = Gst.Buffer.new_wrapped(prefix).append(buf)
        # Keep a decoder pool from recycling the parent while an appsrc/consumer still owns
        # this child, even after the Python GstFrame wrapper has been released.
        buf.add_parent_buffer_meta(self._buffer)
        buf.pts = pts
        buf.dts = Gst.CLOCK_TIME_NONE
        buf.offset = frame_id
        return buf

    def write_to_fd(self, fd: int) -> None:
        # Pre-1.28 unixfdsink requires FD-backed memory and cannot copy/pool it itself.
        # This fallback is only used for frames selected for publication on those runtimes.
        ok, info = self._buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("could not map frame for unixfd transport")
        try:
            data = memoryview(info.data)
            offset = 0
            while offset < len(data):
                n = os.pwrite(fd, data[offset:], offset)
                if n <= 0:
                    raise OSError("could not write frame to memfd")
                offset += n
        finally:
            self._buffer.unmap(info)


FramePayload = Union[bytes, GstFrame]
