#!/usr/bin/env python3
"""Read the active Compose image inventory or launch that exact model with verified IDs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def inventory(model, platform, *, zenohd=False, roscore=False):
    services = model.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("Compose returned no active services")
    roles = {}
    images = set()
    target_arch = os.environ.get("RIG_TARGET_ARCH")
    if target_arch and target_arch not in ("arm64", "amd64"):
        raise ValueError(f"unsupported native replay architecture: {target_arch}")
    native_platforms = {"linux/" + target_arch} if target_arch else set()
    if target_arch == "arm64":
        native_platforms.add("linux/arm64/v8")
    for name, service in services.items():
        ref = service.get("image") if isinstance(service, dict) else None
        if not isinstance(ref, str) or not ref or any(c.isspace() for c in ref):
            raise ValueError(f"active service {name} has no valid runtime image")
        effective_platform = service.get("platform") or os.environ.get("DOCKER_DEFAULT_PLATFORM")
        if target_arch and effective_platform and effective_platform not in native_platforms:
            raise ValueError(f"active service {name} requests {effective_platform}; "
                             f"replay requires native linux/{target_arch}")
        images.add(ref)
        role = ("cam-dev" if platform == "dev" else "cam-core") if name == "core-driver" else name
        if role == "ros2-source":
            role = "ros2-bridge"
        if role in roles and roles[role] != ref:
            raise ValueError(f"logical image {role} resolves to conflicting images")
        roles[role] = ref
    if zenohd:
        roles["shared-zenohd"] = os.environ.get("CAM_ROS2_IMAGE") or "ros2-bridge"
        images.add(roles["shared-zenohd"])
    if roscore:
        roles["shared-roscore"] = os.environ.get("CAM_ROS1_IMAGE") or "ros1-bridge"
        images.add(roles["shared-roscore"])
    return {"version": 1, "images": sorted(images), "roles": roles}


def pinned_model(model, selected, raw_pins):
    try:
        document = json.loads(raw_pins)
    except (TypeError, ValueError) as exc:
        raise ValueError("RIG_RUNTIME_IMAGE_PINS must be a JSON object") from exc
    if (not isinstance(document, dict) or type(document.get("version")) is not int or
            document["version"] != 1 or set(document) != {"version", "images"} or
            not isinstance(document["images"], dict)):
        raise ValueError("RIG_RUNTIME_IMAGE_PINS requires version 1 and an images map")
    pins = document["images"]
    if set(pins) != set(selected["images"]):
        missing = sorted(set(selected["images"]) - set(pins))
        extra = sorted(set(pins) - set(selected["images"]))
        raise ValueError(f"runtime image selection changed (missing pins: {missing}; extra pins: {extra})")
    if any(not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
           for value in pins.values()):
        raise ValueError("runtime image pins must be full sha256 image IDs")
    for service in model["services"].values():
        service["image"] = pins[service["image"]]
        service["pull_policy"] = "never"
        service.pop("build", None)
        if os.environ.get("RIG_TARGET_ARCH"):
            service["platform"] = "linux/" + os.environ["RIG_TARGET_ARCH"]
        # Compose can retain an optional dependency on an inactive profile. Once the
        # active model stands alone, that omitted optional service must not be referenced.
        dependencies = service.get("depends_on", {})
        if isinstance(dependencies, dict):
            for name, dependency in list(dependencies.items()):
                if name not in model["services"]:
                    if not isinstance(dependency, dict) or dependency.get("required") is not False:
                        raise ValueError(f"required Compose dependency {name} is not active")
                    del dependencies[name]
    # Compose's normalized JSON already escapes literal dollars for a second parse.
    return model


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("verb", choices=("runtime-images", "images", "up"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--zenohd", action="store_true")
    parser.add_argument("--roscore", action="store_true")
    args = parser.parse_args(argv[:split])
    compose_args = argv[split + 1:]
    up_args = ["up"]
    if "--" in compose_args:
        split = compose_args.index("--")
        compose_args, up_args = compose_args[:split], compose_args[split + 1:]
    try:
        result = subprocess.run(["docker", "compose", *compose_args, "config", "--format", "json"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError("cannot resolve runtime images: " + result.stderr.strip())
        model = json.loads(result.stdout)
        if not isinstance(model, dict):
            raise ValueError("Compose returned an invalid model")
        selected = inventory(model, args.platform, zenohd=args.zenohd, roscore=args.roscore)
        if args.verb == "runtime-images":
            print(json.dumps(selected, sort_keys=True))
            return 0
        if args.verb == "images":
            # JSON string scalars are valid YAML and safely quote registry/digest references.
            for ref in selected["images"]:
                print("- " + json.dumps(ref))
            return 0
        if args.zenohd or args.roscore:
            raise ValueError("pinned replay cannot start shared --zenohd/--roscore helpers; "
                             "start them as managed infra instances")
        allowed = {"-d", "--detach", "--force-recreate", "--remove-orphans", "--no-deps",
                   "--wait", "--no-build", "--quiet-pull"}
        if not up_args or up_args[0] != "up" or any(item not in allowed for item in up_args[1:]):
            raise ValueError("pinned replay up only accepts launch flags; image policy and service selection are fixed")
        model = pinned_model(model, selected, os.environ.get("RIG_RUNTIME_IMAGE_PINS"))
        # Validate every pin before any daemon mutation, including the external socket volume.
        subprocess.run(["docker", "volume", "create", os.environ["CAM_SOCK_VOLUME"]],
                       check=True, stdout=subprocess.DEVNULL)
        return subprocess.run(["docker", "compose", "--project-directory", str(Path(args.root).resolve()),
                               "-f", "-", *up_args, "--no-build", "--pull", "never"],
                              input=json.dumps(model), text=True).returncode
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"cam-up: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
