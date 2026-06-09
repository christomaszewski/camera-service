#!/usr/bin/env python3
"""Turn one sensor config (YAML) into the env that `cam-up` hands `docker compose`.

The sensor config is the single source of truth. This *selects and parameterizes* — it does not
generate a compose file. It emits `KEY=value` lines:

  COMPOSE_PROJECT_NAME   cam_<name>            (isolates one sensor's containers/networks)
  COMPOSE_PROFILES       <enabled container plugins, comma-separated>
  CAM_INSTANCE          <name>                 (ROS namespace, labels)
  CAM_SOCK_VOLUME       cam_<name>_sock       (stable shm volume name; other stacks attach to it)
  + per-plugin env       (ros2 topic/frame_id/encoding/debayer, webrtc geometry/port), PLUS any
                         UPPERCASE plugin param passed through VERBATIM as an env var (e.g.
                         CAM_WEBRTC_MAX_BITRATE) -- so any bridge knob is YAML-settable without editing this.

Only plugins with `isolation: container` become compose profiles; `isolation: process` plugins are
the supervisor's (in-image) and are ignored here.

Uses PyYAML when it's importable; otherwise falls back to a tiny built-in parser that covers the
(simple, fixed) sensor-config schema, so a vehicle host needs nothing but stock python3.
"""
import sys

try:
    import yaml
    _HAVE_YAML = True
except ImportError:  # pragma: no cover
    _HAVE_YAML = False

_BAYER = {"RG": "rggb", "GR": "grbg", "GB": "gbrg", "BG": "bggr"}


def bayer_pattern(pixel_format: str) -> str:
    """The 4-letter GStreamer Bayer pattern (rggb/grbg/gbrg/bggr) for a Bayer pixel_format, else ''
    (mono/unknown). The webrtc-bridge uses it to debayer a CFA preview to color (video/x-bayer !
    bayer2rgb): on JP7/unixfd the stream caps already carry it, on JP6/raw-shm run.sh applies it."""
    pf = pixel_format or ""
    if not pf.startswith("Bayer") or len(pf) < 7:
        return ""
    return _BAYER.get(pf[5:7].upper(), "")


def ros_bayer_encoding(pixel_format: str) -> str:
    """ROS image encoding for a Bayer pixel format, so the ros2-bridge labels the raw mosaic and a
    downstream image_proc can debayer it. Returns '' for mono/unknown -> the bridge derives
    mono8/mono16 from the frame header instead."""
    pat = bayer_pattern(pixel_format)
    if not pat:
        return ""
    pf = pixel_format or ""
    return "bayer_" + pat + ("16" if any(t in pf for t in ("16", "12", "10")) else "8")


def _scalar(tok):
    """Resolve a YAML scalar the way safe_load would for the value kinds our configs use."""
    t = tok.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if len(t) >= 2 and t[0] in ("'", '"') and t[-1] == t[0]:
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _decomment(raw):
    """Strip a full-line or ` #...` inline comment (our configs never put '#' inside a value)."""
    if raw.lstrip().startswith("#"):
        return ""
    h = raw.find(" #")
    return (raw[:h] if h != -1 else raw).rstrip()


def _load_fallback(text):
    """Stdlib-only parser for OUR sensor-config subset, used only when PyYAML is absent: top-level
    `name`, the `camera`/`gige`/`usb`/`rtsp` maps (flat scalars), and a `plugins` list of maps with
    scalar keys + a nested `params` map. Indent-width agnostic; '#' comments; scalar leaves. NOT general YAML."""
    cfg = {"name": None, "camera": {}, "gige": {}, "usb": {}, "rtsp": {}, "plugins": []}
    section = None       # current top-level section name
    cur = None           # current plugin map
    key_indent = None    # indent of the current plugin's direct keys
    in_params = False
    for raw in text.splitlines():
        line = _decomment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()

        if indent == 0:                                  # top-level: a `key:` section, or a scalar
            cur = None; key_indent = None; in_params = False
            if s.endswith(":"):
                section = s[:-1].strip()
            else:
                section = None
                k, _, v = s.partition(":")
                if k.strip() == "name" and v.strip():
                    cfg["name"] = _scalar(v)
            continue

        if section in ("camera", "gige", "usb", "rtsp"):  # flat scalar keys (type, pixel_format, ...)
            k, _, v = s.partition(":")
            if v.strip():
                cfg[section][k.strip()] = _scalar(v)
            continue
        if section != "plugins":                         # ignore recording/transport/etc. bodies
            continue

        if s.startswith("- "):                           # new plugin list item
            cur = {"params": {}}
            cfg["plugins"].append(cur)
            key_indent = None; in_params = False
            rest = s[2:].strip()
            if ":" in rest:
                k, _, v = rest.partition(":")
                cur[k.strip()] = _scalar(v)
            continue
        if cur is None:
            continue
        if key_indent is None:
            key_indent = indent
        if indent > key_indent:                          # deeper than plugin keys -> a params entry
            if in_params:
                k, _, v = s.partition(":")
                cur["params"][k.strip()] = _scalar(v)
            continue
        in_params = False                                # a direct plugin key
        k, _, v = s.partition(":")
        k = k.strip()
        if k == "params" and v.strip() == "":
            in_params = True
        else:
            cur[k] = _scalar(v)
    return cfg


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: sensor_env.py <sensor-config.yaml>\n")
        return 2
    with open(sys.argv[1]) as f:
        text = f.read()
    cfg = (yaml.safe_load(text) or {}) if _HAVE_YAML else _load_fallback(text)

    name = str(cfg.get("name") or "camera")
    cam = cfg.get("camera") or {}
    stype = str(cam.get("type") or "gige")           # general `camera.type` selects the source block
    src = cfg.get(stype) or {}                        # the active source block (gige/usb/rtsp)
    pixfmt = str(src.get("pixel_format") or "")       # for Bayer derivation (rtsp has none -> '' -> mono path)
    plugins = [p for p in (cfg.get("plugins") or [])
               if isinstance(p, dict) and p.get("name")
               and p.get("enabled", True)
               and str(p.get("isolation", "process")) == "container"]
    by_name = {p["name"]: (p.get("params") or {}) for p in plugins}

    env = {
        "COMPOSE_PROJECT_NAME": f"cam_{name}",
        "COMPOSE_PROFILES": ",".join(p["name"] for p in plugins),
        "CAM_INSTANCE": name,
        "CAM_SOCK_VOLUME": f"cam_{name}_sock",
    }

    ros = by_name.get("ros2-bridge") or by_name.get("ros1-bridge")   # same topic/frame_id/encoding/debayer params
    if ros is not None:
        env["CAM_ROS_TOPIC"] = str(ros.get("topic", "image_raw"))
        env["CAM_FRAME_ID"] = str(ros.get("frame_id", name))
        # A: label a Bayer stream so image_proc can debayer it ('' = mono, derived from the header).
        env["CAM_ROS_ENCODING"] = ros_bayer_encoding(pixfmt)
        # B: optional in-bridge demosaic to rgb8.
        env["CAM_DEBAYER"] = "true" if ros.get("debayer", False) else "false"

    web = by_name.get("webrtc-bridge")
    if web is not None:
        env["CAM_WIDTH"] = str(web.get("width", 512))
        env["CAM_HEIGHT"] = str(web.get("height", 512))
        env["CAM_FORMAT"] = str(web.get("format", "GRAY8"))
        env["CAM_FPS"] = str(web.get("fps", 25))
        env["CAM_SIGNALLING_PORT"] = str(web.get("port", 8443))
        # Discovery (docs/DISCOVERY.md): human-facing role label for the advertised descriptor; default
        # = the sensor name. (vehicle_id/producer_id/signalling default in the bridge from env/hostname.)
        env["CAM_STREAM_ROLE"] = str(web.get("role", name))
        # Bayer pattern from the camera pixel_format -> webrtc debayers the CFA preview to color
        # (bayer2rgb). '' = mono/raw passthrough. JP7/unixfd caps already carry it; JP6/raw-shm
        # run.sh applies it as video/x-bayer caps.
        env["CAM_BAYER"] = bayer_pattern(pixfmt)

    # Generic env passthrough: any UPPERCASE param key on an enabled plugin (e.g. CAM_WEBRTC_MAX_BITRATE,
    # VIDEO_CAPS, GST_PLUGIN_FEATURE_RANK) is emitted VERBATIM as an env var -- so ANY bridge env knob is
    # settable straight from the sensor YAML without teaching this file about it. Lowercase param keys keep
    # the friendly per-plugin mapping above. Runs last, so an explicit UPPERCASE key overrides a friendly default.
    for params in by_name.values():
        for k, v in params.items():
            if k.isupper():
                env[k] = str(v)

    for k, v in env.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
