#!/usr/bin/env python3
"""Turn one sensor config (YAML) into the env that `gige-up` hands `docker compose`.

The sensor config is the single source of truth. This *selects and parameterizes* — it does not
generate a compose file. It emits `KEY=value` lines:

  COMPOSE_PROJECT_NAME   gige_<name>            (isolates one sensor's containers/networks)
  COMPOSE_PROFILES       <enabled container plugins, comma-separated>
  GIGE_INSTANCE          <name>                 (ROS namespace, labels)
  GIGE_SOCK_VOLUME       gige_<name>_sock       (stable shm volume name; other stacks attach to it)
  + per-plugin env       (ros2 topic/frame_id, webrtc geometry/port)

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
    `name`, and a `plugins` list of maps with scalar keys + a nested `params` map. Indent-width
    agnostic; '#' comments; scalar leaves. NOT general YAML — it only needs to feed main()."""
    cfg = {"name": None, "plugins": []}
    in_plugins = False
    cur = None          # current plugin map
    key_indent = None   # indent of the current plugin's direct keys
    in_params = False
    for raw in text.splitlines():
        line = _decomment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()

        if indent == 0:                                  # top-level key
            in_plugins = (s == "plugins:")
            cur = None; key_indent = None; in_params = False
            if not in_plugins and ":" in s:
                k, _, v = s.partition(":")
                if k.strip() == "name" and v.strip():
                    cfg["name"] = _scalar(v)
            continue
        if not in_plugins:                               # ignore camera/recording/transport bodies
            continue
        if s.startswith("- "):                           # new plugin list item
            cur = {"params": {}}
            cfg["plugins"].append(cur)
            key_indent = None; in_params = False
            rest = s[2:].strip()                         # usually "name: <x>" inline
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
    plugins = [p for p in (cfg.get("plugins") or [])
               if isinstance(p, dict) and p.get("name")
               and p.get("enabled", True)
               and str(p.get("isolation", "process")) == "container"]
    by_name = {p["name"]: (p.get("params") or {}) for p in plugins}

    env = {
        "COMPOSE_PROJECT_NAME": f"gige_{name}",
        "COMPOSE_PROFILES": ",".join(p["name"] for p in plugins),
        "GIGE_INSTANCE": name,
        "GIGE_SOCK_VOLUME": f"gige_{name}_sock",
    }

    ros = by_name.get("ros2-bridge")
    if ros is not None:
        env["GIGE_ROS_TOPIC"] = str(ros.get("topic", "image_raw"))
        env["GIGE_FRAME_ID"] = str(ros.get("frame_id", name))

    web = by_name.get("webrtc-bridge")
    if web is not None:
        env["GIGE_WIDTH"] = str(web.get("width", 512))
        env["GIGE_HEIGHT"] = str(web.get("height", 512))
        env["GIGE_FORMAT"] = str(web.get("format", "GRAY8"))
        env["GIGE_FPS"] = str(web.get("fps", 25))
        env["GIGE_SIGNALLING_PORT"] = str(web.get("port", 8443))

    for k, v in env.items():
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
