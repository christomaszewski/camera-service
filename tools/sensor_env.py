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
the supervisor's (in-image) and are ignored here. Needs PyYAML (apt: python3-yaml).
"""
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("sensor_env: PyYAML required on the host (apt install python3-yaml)\n")
    sys.exit(2)


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: sensor_env.py <sensor-config.yaml>\n")
        return 2
    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f) or {}

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
