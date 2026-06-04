"""Configuration model loaded from YAML.

Unknown keys in the YAML are ignored (with the dataclass defaults applied) so the
config file can carry forward-looking knobs without breaking older code.

One file, two consumers: the `camera` / `recording` / `preview` / `transport`
sections drive the core pipeline; the `plugins` list is for the (future) per-sensor
supervisor that spawns each enabled plugin as its own process.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Optional


@dataclass
class ROI:
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


@dataclass
class CameraConfig:
    camera_id: Optional[str] = None          # serial / name; None = first device found
    fake: bool = False                        # use Aravis in-process "Fake" camera (no hardware/network)
    pixel_format: Optional[str] = "Mono8"
    roi: Optional[ROI] = None
    frame_rate: Optional[float] = None
    packet_size: int = 9000
    packet_delay: Optional[int] = None
    n_stream_buffers: int = 20

    timestamp_source: str = "ptp_chunk"       # ptp_chunk | camera | system
    ptp_enable: bool = True
    ptp_mode: str = "SlaveOnly"
    ptp_lock_timeout_s: float = 10.0
    chunk_selectors: str = "Timestamp,FrameID"
    chunk_timestamp_name: str = "ChunkTimestamp"
    chunk_frame_id_name: str = "ChunkFrameID"

    # Reconnect/backoff: recover from a camera dropping off the link without dying or
    # corrupting the recording. The pipeline stays up; a watchdog re-opens the camera.
    reconnect: bool = True
    reconnect_timeout_s: float = 3.0        # no buffer for this long => treat as disconnected
    reconnect_backoff_s: float = 1.0        # initial delay between re-open attempts
    reconnect_backoff_max_s: float = 30.0   # exponential backoff cap


@dataclass
class RecordingConfig:
    enabled: bool = True
    encoder: str = "auto"
    output_dir: str = "/data/recordings"
    name_prefix: str = "gige"
    segment_seconds: int = 60
    bayer_pattern: Optional[str] = None
    # Pre-encode CFA tiling: deinterleave an 8-bit Bayer mosaic into 4 quadrant sub-planes so the
    # lossless codec's spatial + temporal prediction stops fighting the CFA checkerboard. Recorder-only
    # (transport/preview/raw still see the mosaic); playback must untile (flagged in the sidecar).
    bayer_tile: bool = False
    # Temporal-compression window: the I-frame ("keyframe") interval in SECONDS -> the longest span the
    # encoder can predict across. Bigger = smaller files but coarser seek + less corruption resilience.
    # 0 = leave the encoder default. For archival lossless, set this to your segment length (or a few
    # seconds for a seek/size balance). Ignored by ffv1 (intra-only). Maps to iframeinterval / x265 keyint.
    keyframe_interval_s: float = 0.0
    # B-frames between reference frames (0 = P-only / lowest latency; more = better compression). HW
    # lossless B-frame support is encoder/firmware-dependent -- verify on-device. Ignored by ffv1.
    bframes: int = 0


@dataclass
class PreviewConfig:
    enabled: bool = False
    sink: str = "fakesink sync=false"


@dataclass
class TransportEndpoint:
    enabled: bool = False
    socket_path: str = ""
    shm_size: int = 0             # bytes for the shm area; 0 = auto (frame_size * 8)
    max_rate_hz: float = 0.0      # publish-rate cap (plugin endpoint); 0 = every frame


@dataclass
class TransportConfig:
    # `application/x-gige-frame` endpoint (36-byte header + frame) for out-of-process plugins:
    plugin_endpoint: TransportEndpoint = field(
        default_factory=lambda: TransportEndpoint(enabled=True, socket_path="/tmp/gige/frames"))
    # optional clean `video/x-raw` endpoint for generic same-host tools (no metadata):
    raw_endpoint: TransportEndpoint = field(
        default_factory=lambda: TransportEndpoint(enabled=False, socket_path="/tmp/gige/raw"))


@dataclass
class PluginConfig:
    name: str
    enabled: bool = True
    command: Optional[list] = None    # explicit launch command (list of args); else resolved by name
    restart: bool = True              # restart this plugin if it exits unexpectedly
    # "process" = lightweight, spawned in-image by the supervisor; "container" = a heavy
    # plugin with its own image, run as a compose sibling (the launcher maps it to a profile).
    isolation: str = "process"
    params: dict = field(default_factory=dict)   # plugin-specific (e.g. ROS params); consumed by the supervisor


@dataclass
class AppConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    plugins: list = field(default_factory=list)   # list[PluginConfig], for the plugin supervisor


def _build(cls, data):
    """Instantiate a dataclass from a dict, ignoring unknown keys."""
    data = data or {}
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _merge_endpoint(data, default: TransportEndpoint) -> TransportEndpoint:
    """Overlay provided keys onto a default endpoint (keeps default enabled/socket_path)."""
    if not data:
        return default
    known = {f.name for f in fields(TransportEndpoint)}
    merged = {f.name: getattr(default, f.name) for f in fields(TransportEndpoint)}
    merged.update({k: v for k, v in data.items() if k in known})
    return TransportEndpoint(**merged)


def parse_config(raw: dict) -> AppConfig:
    """Build the config from an already-parsed dict (no YAML dependency; unit-testable)."""
    raw = raw or {}
    cam_raw = dict(raw.get("camera", {}) or {})
    roi_raw = cam_raw.pop("roi", None)
    camera = _build(CameraConfig, cam_raw)
    camera.roi = _build(ROI, roi_raw) if roi_raw else None

    defaults = TransportConfig()
    tr_raw = dict(raw.get("transport", {}) or {})
    transport_cfg = TransportConfig(
        plugin_endpoint=_merge_endpoint(tr_raw.get("plugin_endpoint"), defaults.plugin_endpoint),
        raw_endpoint=_merge_endpoint(tr_raw.get("raw_endpoint"), defaults.raw_endpoint),
    )

    plugins = []
    for p in (raw.get("plugins") or []):
        if not isinstance(p, dict) or "name" not in p:
            continue
        reserved = ("name", "enabled", "command", "restart", "isolation")
        plugins.append(PluginConfig(
            name=p["name"], enabled=bool(p.get("enabled", True)),
            command=p.get("command"), restart=bool(p.get("restart", True)),
            isolation=str(p.get("isolation", "process")),
            params={k: v for k, v in p.items() if k not in reserved}))

    return AppConfig(
        camera=camera,
        recording=_build(RecordingConfig, raw.get("recording")),
        preview=_build(PreviewConfig, raw.get("preview")),
        transport=transport_cfg,
        plugins=plugins,
    )


def load_config(path: str) -> AppConfig:
    import yaml  # imported lazily so the module is usable without PyYAML installed
    with open(path) as f:
        return parse_config(yaml.safe_load(f) or {})
