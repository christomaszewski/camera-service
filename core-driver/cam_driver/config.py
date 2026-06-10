"""Configuration model loaded from YAML.

Unknown keys in the YAML are ignored (with the dataclass defaults applied) so the
config file can carry forward-looking knobs without breaking older code.

Schema (symmetric across source types):
  camera:   GENERAL settings + `type` (gige|usb|rtsp) -> selects the source frontend.
  gige:/usb:/rtsp:   the SELECTED source's specifics.
The `camera.frame_rate` + reconnect knobs are general; parse_config overlays them onto the active
source's effective config, so the source code reads them from its own block while the YAML sets them
ONCE under `camera:`.

One file, two consumers: the `camera`/`<source>`/`recording`/`preview`/`transport` sections drive the
core pipeline; the `plugins` list is for the per-sensor supervisor / cam-up (spawns each enabled plugin).
"""
from __future__ import annotations

import functools
import os
import time
import typing
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
    """GENERAL camera settings, shared by every source. `type` selects the source frontend; per-source
    specifics live in the matching gige:/usb:/rtsp: block. frame_rate + the reconnect knobs are general
    -- parse_config overlays them onto the active source's config (the source reads them from its own
    block; the YAML sets them once here)."""
    type: str = "gige"                       # gige (GVSP/Aravis) | usb (v4l2) | rtsp
    frame_rate: Optional[float] = None       # target/delivered fps: gige requests it, usb pins it, rtsp informational
    # Reconnect/backoff: recover from a source dropping/stalling without dying or corrupting the recording
    # -- the pipeline stays up while a watchdog re-opens the source (gige: control-lost/no-buffer; usb:
    # unplug/stall; rtsp: stalled stream).
    reconnect: bool = True
    reconnect_timeout_s: float = 5.0         # no data for this long => disconnected -> reopen
    reconnect_backoff_s: float = 1.0         # initial delay between re-open attempts
    reconnect_backoff_max_s: float = 30.0    # exponential backoff cap


@dataclass
class GigeConfig:
    """GVSP/Aravis source params (camera.type == gige)."""
    camera_id: Optional[str] = None          # serial / name; None = first device found
    fake: bool = False                        # use Aravis in-process "Fake" camera (no hardware/network)
    pixel_format: Optional[str] = "Mono8"
    roi: Optional[ROI] = None
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

    # General settings -- set in the `camera:` block; parse_config overlays them here (NOT per-source YAML).
    frame_rate: Optional[float] = None
    reconnect: bool = True
    reconnect_timeout_s: float = 5.0


@dataclass
class UsbConfig:
    """USB / v4l2 source params (camera.type == usb)."""
    device: str = "/dev/video0"       # prefer a stable /dev/v4l/by-id/... path on real hardware
    fake: bool = False                # videotestsrc instead of v4l2src (CI/dev; no device needed)
    pixel_format: str = "GRAY8"       # delivered format: raw (GRAY8/YUY2/I420/...) or encoded (MJPEG/H264/H265)
    width: int = 640
    height: int = 480
    sof_timestamps: bool = False      # use the v4l2 DRIVER per-frame timestamp (do-timestamp=false ->
    #   'sof' provenance) instead of host arrival. OPT-IN: the gain is camera-dependent -- a cam that
    #   timestamps at start-of-exposure wins; many cheap UVC cams timestamp at dequeue (== arrival, no
    #   gain) or report zeros (we sanity-check and fall back to arrival). Ignored for fake sources.

    # General settings -- set in the `camera:` block; parse_config overlays them here (NOT per-source YAML).
    frame_rate: float = 30.0
    reconnect: bool = True            # hotplug/stall recovery: a data-starvation watchdog reopens the v4l2
    #   device when frames stop (unplug, stall, or absent at startup). Use a STABLE /dev/v4l/by-id/... path.
    reconnect_timeout_s: float = 5.0


@dataclass
class RtspConfig:
    """RTSP source params (camera.type == rtsp). Always encoded -> stream-copy recording + decode for
    consumers, with RTCP->NTP per-frame provenance on gst>=1.24."""
    url: str = "rtsp://127.0.0.1:8554/test"
    probe: bool = True                # `rtspsrc ! parsebin` probe at open() for codec+geometry (the source
    #                                   of truth). codec/width/height below are FALLBACKS (probe fail/off).
    codec: str = "h264"               # h264 | h265 | mjpeg (fallback; the probe overrides)
    latency_ms: int = 200             # rtspsrc jitter-buffer latency
    width: int = 640                  # fallback (the probe overrides)
    height: int = 480                 # fallback (the probe overrides)
    protocols: str = ""               # rtspsrc transport: "" = default (udp w/ tcp fallback),
    #                                   "tcp" forces RTP-over-TCP (firewalls / Docker NAT / lossy nets)
    decoder: str = "auto"             # consumer decode branch: auto = HW (NVDEC) when present |
    #                                   software = force avdec, for streams HW decode can't start on
    #                                   (no-IDR/intra-refresh cameras; see formats.select_decoder)

    # General settings -- set in the `camera:` block; parse_config overlays them here (NOT per-source YAML).
    frame_rate: float = 30.0          # informational; the stream sets the real rate
    reconnect: bool = True            # auto-recover a stalled stream (camera ACKs PLAY then streams no media)
    reconnect_timeout_s: float = 5.0


@dataclass
class RecordingConfig:
    enabled: bool = True
    encoder: str = "auto"
    output_dir: str = "/data/recordings"
    name_prefix: str = "cam"
    segment_seconds: int = 60
    bayer_pattern: Optional[str] = None
    # Pre-encode CFA tiling: deinterleave an 8-bit Bayer mosaic into 4 quadrant sub-planes so the lossless
    # codec's spatial + temporal prediction stops fighting the CFA checkerboard. Recorder-only
    # (transport/preview/raw still see the mosaic); playback must untile (mode is flagged in the sidecar).
    #   off | plain (== true) | green_diff | rct
    # plain = quadrant tiling (the big win); green_diff = + (Gb-Gr) residual (safe, near-free); rct = +
    # a reversible R-G/B-G/Gb-Gr colour transform (higher ceiling, but the 8-bit wrap taxes a predictive
    # HW codec -- measure it). See cam_driver.bayer_tile. A plain `true`/`false` still works.
    bayer_tile: str = "off"
    # Temporal-compression window: the I-frame ("keyframe") interval in SECONDS -> the longest span the
    # encoder can predict across. Bigger = smaller files but coarser seek + less corruption resilience.
    # 0 = leave the encoder default. For archival lossless, set this to your segment length (or a few
    # seconds for a seek/size balance). Ignored by ffv1 (intra-only). Maps to iframeinterval / x265 keyint.
    keyframe_interval_s: float = 0.0
    # B-frames between reference frames (0 = P-only / lowest latency; more = better compression). HW
    # lossless B-frame support is encoder/firmware-dependent (on the Orin nvv4l2enc it's Xavier-only =
    # a no-op). Ignored by ffv1.
    bframes: int = 0
    # NVENC (hw-hevc-lossless) tuning. preset-level controls how hard the HW encoder searches for a
    # compact LOSSLESS representation: bigger = smaller files but slower encode -- watch real-time, a
    # 5MP@fps recorder must keep up or it drops frames. "" leaves the encoder default (UltraFast = the
    # FASTEST + LARGEST). Accepts ultrafast(1)|fast(2)|medium(3)|slow(4) or the number.
    nvenc_preset: str = ""
    nvenc_maxperf: bool = True   # maxperf-enable (deprecated; high GPU clocks for throughput)
    # The GRAY8->NV24 (or ->I420) CPU `videoconvert` is the recorder's real per-frame bottleneck at high
    # res (single-threaded ~21fps for 5MP in a 30W power mode -- right at the 24fps line). Parallelize it
    # across cores so the recorder keeps real-time with margin (5MP: n-threads=6 -> ~69fps). 0 = all cores.
    videoconvert_threads: int = 4


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
    # `application/x-cam-frame` endpoint (36-byte header + frame) for out-of-process plugins:
    plugin_endpoint: TransportEndpoint = field(
        default_factory=lambda: TransportEndpoint(enabled=True, socket_path="/tmp/cam/frames"))
    # optional clean `video/x-raw` endpoint for generic same-host tools (no metadata):
    raw_endpoint: TransportEndpoint = field(
        default_factory=lambda: TransportEndpoint(enabled=False, socket_path="/tmp/cam/raw"))


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
    camera: CameraConfig = field(default_factory=CameraConfig)   # GENERAL settings + source `type`
    gige: GigeConfig = field(default_factory=GigeConfig)         # gige source params
    usb: UsbConfig = field(default_factory=UsbConfig)            # usb source params
    rtsp: RtspConfig = field(default_factory=RtspConfig)         # rtsp source params
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    transport: TransportConfig = field(default_factory=TransportConfig)
    plugins: list = field(default_factory=list)   # list[PluginConfig], for the plugin supervisor


@functools.lru_cache(maxsize=None)
def _hints(cls):
    """Resolve a dataclass's (PEP 563 string) annotations to real types, once per class."""
    return typing.get_type_hints(cls)


def _numeric_kind(hint):
    """Return int/float if `hint` is that numeric type (incl. Optional[...]), else None.
    bool is intentionally NOT numeric here: it subclasses int, so int(True)==1 would otherwise
    coerce a stray boolean silently."""
    args = typing.get_args(hint)
    if args:                                   # Optional[X] == Union[X, None]
        non_none = [a for a in args if a is not type(None)]
        hint = non_none[0] if len(non_none) == 1 else None
    return hint if hint in (int, float) else None


def _coerce(cls, key, value):
    """Coerce `value` to field `key`'s annotated numeric type, with a clear, field-named error.
    Non-numeric fields (and None) pass through untouched. This turns a YAML typo like
    `height: 1080p` into a fast, legible `UsbConfig.height: expected int, got '1080p'` at parse
    time -- instead of a cryptic int() crash deep in a source that then looks like a downstream
    transport/bridge failure (the bug that motivated this)."""
    kind = _numeric_kind(_hints(cls).get(key))
    if kind is None or value is None:
        return value
    if isinstance(value, bool):                # see _numeric_kind: bool is not a valid numeric here
        raise ValueError(f"{cls.__name__}.{key}: expected {kind.__name__}, got bool {value!r}")
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ValueError(f"{cls.__name__}.{key}: expected {kind.__name__}, got {value!r}") from None


def _build(cls, data):
    """Instantiate a dataclass from a dict, ignoring unknown keys and coercing numeric fields."""
    data = data or {}
    known = {f.name for f in fields(cls)}
    return cls(**{k: _coerce(cls, k, v) for k, v in data.items() if k in known})


def _merge_endpoint(data, default: TransportEndpoint) -> TransportEndpoint:
    """Overlay provided keys onto a default endpoint (keeps default enabled/socket_path)."""
    if not data:
        return default
    known = {f.name for f in fields(TransportEndpoint)}
    merged = {f.name: getattr(default, f.name) for f in fields(TransportEndpoint)}
    merged.update({k: _coerce(TransportEndpoint, k, v) for k, v in data.items() if k in known})
    return TransportEndpoint(**merged)


def parse_config(raw: dict) -> AppConfig:
    """Build the config from an already-parsed dict (no YAML dependency; unit-testable)."""
    raw = raw or {}
    camera = _build(CameraConfig, raw.get("camera"))   # general (type + shared settings)

    gige_raw = dict(raw.get("gige", {}) or {})
    roi_raw = gige_raw.pop("roi", None)
    gige = _build(GigeConfig, gige_raw)
    gige.roi = _build(ROI, roi_raw) if roi_raw else None
    usb = _build(UsbConfig, raw.get("usb"))
    rtsp = _build(RtspConfig, raw.get("rtsp"))

    # Overlay the GENERAL camera settings onto each source's effective config -- the source code reads
    # frame_rate/reconnect from its own block, but the YAML sets them ONCE under `camera:`. frame_rate
    # only overrides when actually given (else each source keeps its sensible default).
    for sc in (gige, usb, rtsp):
        if camera.frame_rate is not None:
            sc.frame_rate = camera.frame_rate
        sc.reconnect = camera.reconnect
        sc.reconnect_timeout_s = camera.reconnect_timeout_s

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
        reserved = ("name", "enabled", "command", "restart", "isolation", "params")
        # Params live in a nested `params:` map (the documented shape, what every sensor config and
        # tools/sensor_env.py use); bare top-level extras are still collected as a fallback.
        nested = p.get("params") if isinstance(p.get("params"), dict) else {}
        flat = {k: v for k, v in p.items() if k not in reserved}
        plugins.append(PluginConfig(
            name=p["name"], enabled=bool(p.get("enabled", True)),
            command=p.get("command"), restart=bool(p.get("restart", True)),
            isolation=str(p.get("isolation", "process")),
            params={**flat, **nested}))

    return AppConfig(
        camera=camera,
        gige=gige,
        usb=usb,
        rtsp=rtsp,
        recording=_build(RecordingConfig, raw.get("recording")),
        preview=_build(PreviewConfig, raw.get("preview")),
        transport=transport_cfg,
        plugins=plugins,
    )


def resolve_recording_dir(output_dir: str, rig_data_dir: str = "", instance: str = "") -> str:
    """Resolve the recording dir for a deploy (cam-up/rig set the env; a bare run passes nothing).

    rig exports RIG_DATA_DIR -- an ABSOLUTE host data root, bind-mounted into the core at the same path --
    so rooting the recording dir there keeps recordings OFF the repo, and a `rig bake` leaves the absolute
    path literal instead of pulling it into the deployment artifact. cam-up exports CAM_INSTANCE, which
    namespaces a per-sensor subdir so cameras sharing one data dir don't collide. Only the DEFAULT
    ('/data/recordings') is transformed; an explicitly-pinned output_dir is returned untouched."""
    if output_dir != "/data/recordings":
        return output_dir                                  # explicit pin -> respect it as-is
    rdd = (rig_data_dir or "").strip().rstrip("/")
    base = f"{rdd}/recordings" if rdd else "/data/recordings"
    inst = (instance or "").strip()
    return f"{base}/{inst}" if inst else base


def unique_run_prefix(output_dir: str, name_prefix: str, _now=None) -> str:
    """Return a per-RUN recording prefix `<name_prefix>-<UTC stamp>` that collides with nothing
    already in `output_dir`.

    The recorder writes `<prefix>-%05d.mkv` (splitmuxsink restarts numbering at 00000) and the
    sidecar truncates `<prefix>.csv`/`.json` on open -- so a fixed prefix means every service
    restart (e.g. compose `restart: unless-stopped` after a crash) OVERWRITES the previous run's
    data. Stamping the prefix per run turns restarts into new runs side by side. A restart loop
    faster than the 1 s stamp resolution gets a `.N` suffix instead of a collision."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(_now))
    base = f"{name_prefix}-{stamp}"
    try:
        existing = os.listdir(output_dir)
    except OSError:
        return base                                        # dir not there yet -> nothing to collide with
    candidate, n = base, 1
    while any(e.startswith(candidate) for e in existing):
        n += 1
        candidate = f"{base}.{n}"
    return candidate


def load_config(path: str) -> AppConfig:
    import yaml  # imported lazily so the module is usable without PyYAML installed
    with open(path) as f:
        return parse_config(yaml.safe_load(f) or {})
