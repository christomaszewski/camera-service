"""GigE Vision core driver: capture + hardware-timestamp extraction + lossless recording.

See the module layout:
  config      - YAML-backed configuration model
  camera      - Aravis camera setup (discovery, features, PTP, chunk mode, stream)
  timestamps  - per-frame timestamp extraction with a PTP-first fallback ladder
  sidecar     - threaded CSV writer + JSON header describing the recording
  recorder    - pluggable lossless recorder branch (HW HEVC-lossless / FFV1 / x265)
  pipeline    - GStreamer assembly: Aravis-fed appsrc -> tee -> [recorder][preview]
"""

__version__ = "0.0.1"
