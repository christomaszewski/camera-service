"""frame-capture: an in-image plugin that keeps a short buffer of recent frames and, on a trigger over
Zenoh, writes the frame nearest a requested time as a JPEG (with GPS EXIF when the trigger carries a
fix) plus a JSON sidecar. Contract: docs/CAPTURE.md.

Modules, pure first (each unit-tested without GStreamer or a zenoh binding):
  timebase  per-frame time normalization: capture stamp (UTC or TAI) vs the plugin's own arrival time
  frames    the time-bounded ring buffer and nearest-frame selection
  request   trigger parsing / validation, GPS extrapolation to the chosen frame
  exif      a stdlib EXIF (APP1) writer and the JPEG splice -- no re-encode
  encode    GStreamer: frame -> 8-bit -> JPEG (Bayer demosaic, 16-bit scaling, optional downscale)
  reader    GStreamer: the core's plugin endpoint (JP7 unixfd / JP6 shm + header) -> frames
  sinks     where a capture goes: the camera's run directory (file) and/or the vehicle store (zenoh)
  service   the request pipeline (select -> encode -> write -> reply) and its zenoh adapter
"""
