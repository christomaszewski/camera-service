#!/usr/bin/env python3
"""Exercise the launcher against real Compose rendering; intercept every daemon mutation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
import runtime_images


REPO = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker")


@unittest.skipUnless(DOCKER, "Docker Compose is required for offline rendering")
class RuntimeImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="camera_images_")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.log = self.folder / "commands.jsonl"
        self.config = self.folder / "sensor.yaml"
        self.write_config()
        fake = self.folder / "docker"
        fake.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
entry = {"args": args}
if args[:1] == ["compose"] and "up" in args:
    entry["model"] = json.load(sys.stdin)
with open(os.environ["CAM_TEST_COMMANDS"], "a") as out:
    out.write(json.dumps(entry) + "\\n")
if args[:2] == ["compose", "version"] or (args[:1] == ["compose"] and "config" in args):
    raise SystemExit(subprocess.run([os.environ["CAM_REAL_DOCKER"], *args]).returncode)
if args[:2] != ["volume", "create"] and "model" not in entry:
    raise SystemExit("unexpected daemon operation: " + str(args))
''')
        fake.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CAM_", "RIG_", "COMPOSE_")) and k != "DOCKER_DEFAULT_PLATFORM"}
        self.env.update(CAM_PYTHON=sys.executable, CAM_REAL_DOCKER=DOCKER,
                        CAM_TEST_COMMANDS=str(self.log), PATH=f"{self.folder}:{os.environ['PATH']}")

    def write_config(self, plugins=None):
        self.config.write_text(json.dumps({"service": "camera-service", "name": "image_test",
            "camera": {"type": "gige"}, "gige": {"fake": True},
            "plugins": plugins if plugins is not None else [
                {"name": "ros2-bridge", "enabled": True, "isolation": "container"},
                {"name": "webrtc-bridge", "enabled": False, "isolation": "container"}]}))

    def launch(self, verb, *, platform="dev", extra_flags=(), extra_args=(), repo=REPO, **env):
        result = subprocess.run([str(repo / "cam-up"), "--" + platform, *extra_flags,
                                 str(self.config), verb, *extra_args], env=self.env | env,
                                text=True, capture_output=True)
        return result

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def query(self, **kwargs):
        result = self.launch("runtime-images", **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def pins(self, inventory):
        return {ref: "sha256:" + f"{i:064x}" for i, ref in enumerate(inventory["images"], 1)}

    def test_disabled_profiles_and_shared_image_roles(self):
        result = self.query()
        self.assertEqual(result, {"version": 1, "images": ["cam-dev:dev", "ros2-bridge:dev"],
                                 "roles": {"cam-dev": "cam-dev:dev", "ros2-bridge": "ros2-bridge:dev"}})
        self.write_config([{"name": "ros2-source", "enabled": True, "isolation": "container"},
                           {"name": "ros2-bridge", "enabled": True, "isolation": "container"}])
        self.assertEqual(self.query()["images"], result["images"])

    def test_custom_overrides_platform_roles_and_registry_tags(self):
        for platform, core, variable in (("dev", "cam-dev", "CAM_DEV_IMAGE"),
                                         ("jp6", "cam-core", "CAM_CORE_IMAGE"),
                                         ("jp6m", "cam-core", "CAM_CORE_IMAGE"),
                                         ("jp7", "cam-core", "CAM_CORE_IMAGE")):
            with self.subTest(platform=platform):
                result = self.query(platform=platform, **{variable: "localhost:5000/custom:testing"},
                                    CAM_ROS2_IMAGE="other/ros:custom", RIG_IMAGE_REGISTRY="ignored:5000")
                self.assertEqual(result["roles"], {core: "localhost:5000/custom:testing",
                                                   "ros2-bridge": "other/ros:custom"})
        result = self.query(RIG_IMAGE_REGISTRY="localhost:5000", RIG_IMAGE_TAG="v1")
        self.assertEqual(result["images"], ["localhost:5000/cam-dev:v1-dev",
                                            "localhost:5000/ros2-bridge:v1-dev"])

    def test_queries_never_mutate_even_with_standalone_helper_flags(self):
        before = self.config.read_bytes()
        result = self.query(extra_flags=("--zenohd", "--roscore"))
        self.assertIn("shared-zenohd", result["roles"])
        self.assertIn("shared-roscore", result["roles"])
        yaml_result = self.launch("images")
        self.assertEqual(yaml_result.returncode, 0, yaml_result.stderr)
        self.assertEqual(yaml.safe_load(yaml_result.stdout), self.query()["images"])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual({p.name for p in self.folder.iterdir()}, {"docker", "commands.jsonl", "sensor.yaml"})
        for call in self.commands():
            self.assertEqual(call["args"][0], "compose")
            self.assertTrue("config" in call["args"] or "version" in call["args"])

    def test_pinned_up_freezes_exact_model_without_build_or_pull(self):
        selected = self.query(CAM_DEV_IMAGE="custom:core")
        pins = self.pins(selected)
        result = self.launch("up", extra_args=("-d",), CAM_DEV_IMAGE="custom:core",
                             RIG_DATA_DIR="/tmp/dollar$literal", RIG_TARGET_ARCH="arm64",
                             RIG_RUNTIME_IMAGE_PINS=json.dumps({"version": 1, "images": pins}))
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertEqual(commands[-2]["args"][:2], ["volume", "create"])
        launch = commands[-1]
        self.assertEqual(launch["args"][-5:], ["up", "-d", "--no-build", "--pull", "never"])
        self.assertEqual(launch["args"][1:5], ["--project-directory", str(REPO), "-f", "-"])
        services = launch["model"]["services"]
        self.assertEqual(set(services), {"core-driver", "ros2-bridge"})
        self.assertEqual(services["core-driver"]["image"], pins["custom:core"])
        for service in services.values():
            self.assertNotIn("build", service)
            self.assertEqual(service["pull_policy"], "never")
            self.assertEqual(service["platform"], "linux/arm64")
        # Compose parses the frozen stdin model once more; dollars must remain literal.
        rendered = subprocess.run([DOCKER, "compose", "-f", "-", "config", "--format", "json"],
            input=json.dumps(launch["model"]), env=self.env | {"COMPOSE_PROFILES": "ros2-bridge"},
            text=True, capture_output=True, check=True)
        core = json.loads(rendered.stdout)["services"]["core-driver"]
        self.assertEqual(core["environment"]["RIG_DATA_DIR"],
                         services["core-driver"]["environment"]["RIG_DATA_DIR"])
        self.assertEqual(core["environment"]["RIG_DATA_DIR"], "/tmp/dollar$$literal")

    def test_invalid_or_changed_pins_refuse_before_any_daemon_mutation(self):
        pins = self.pins(self.query())
        bad = ["", "invalid", "null", "[]", json.dumps({"version": True, "images": pins}),
               json.dumps({"version": 1, "images": {}}),
               json.dumps({"version": 1, "images": pins | {"unexpected": "sha256:" + "a" * 64}}),
               json.dumps({"version": 1, "images": {key: "tag:mutable" for key in pins}})]
        for document in bad:
            with self.subTest(document=document):
                self.log.write_text("")
                result = self.launch("up", RIG_RUNTIME_IMAGE_PINS=document)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(all(call["args"][0] == "compose" and "up" not in call["args"]
                                    for call in self.commands()))
        result = self.launch("up", CAM_DEV_IMAGE="changed:core",
                             RIG_RUNTIME_IMAGE_PINS=json.dumps({"version": 1, "images": pins}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selection changed", result.stderr)

    def test_pins_reject_image_selection_startup_overrides_and_unmanaged_helpers(self):
        pins = json.dumps({"version": 1, "images": self.pins(self.query())})
        for extra_args, extra_flags in ((("--build",), ()), (("--pull", "always"), ()),
                                        (("--pull=always",), ()), (("--build=true",), ()),
                                        (("--no-recreate",), ()), (("--no-start",), ()),
                                        (("core-driver",), ()), (("--scale", "core-driver=0"), ()),
                                        ((), ("--zenohd",)),
                                        ((), ("--roscore",))):
            with self.subTest(extra_args=extra_args, extra_flags=extra_flags):
                self.log.write_text("")
                result = self.launch("up", extra_args=extra_args, extra_flags=extra_flags,
                                     RIG_RUNTIME_IMAGE_PINS=pins)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(all(call["args"][0] == "compose" and "up" not in call["args"]
                                    for call in self.commands()))

    def test_vendored_surface_includes_query_helper(self):
        staging = self.folder / "vendored"
        descriptor = yaml.safe_load((REPO / "rigging.yaml").read_text())
        for rel in descriptor["launch_surface"]:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, dest)
        self.assertEqual(self.query(repo=staging)["images"], self.query()["images"])

    def test_native_architecture_rejects_foreign_service_or_ambient_platform(self):
        result = self.launch("runtime-images", RIG_TARGET_ARCH="arm64",
                             DOCKER_DEFAULT_PLATFORM="linux/amd64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires native linux/arm64", result.stderr)
        self.query(RIG_TARGET_ARCH="arm64", DOCKER_DEFAULT_PLATFORM="linux/arm64")
        model = {"services": {"core-driver": {"image": "custom:core", "platform": "linux/amd64"}}}
        with patch.dict(os.environ, {"RIG_TARGET_ARCH": "arm64"}):
            with self.assertRaisesRegex(ValueError, "requires native linux/arm64"):
                runtime_images.inventory(model, "dev")
            model["services"]["core-driver"]["platform"] = "linux/arm64/v8"
            self.assertEqual(runtime_images.inventory(model, "dev")["roles"], {"cam-dev": "custom:core"})

    def test_frozen_active_model_prunes_only_optional_missing_dependencies(self):
        model = {"services": {"core-driver": {"image": "custom:core", "depends_on": {
            "inactive": {"condition": "service_started", "required": False}}}}}
        selected = {"images": ["custom:core"]}
        pins = json.dumps({"version": 1, "images": self.pins(selected)})
        frozen = runtime_images.pinned_model(model, selected, pins)
        self.assertEqual(frozen["services"]["core-driver"]["depends_on"], {})
        model = {"services": {"core-driver": {"image": "custom:core", "depends_on": {
            "missing": {"condition": "service_started", "required": True}}}}}
        with self.assertRaisesRegex(ValueError, "required Compose dependency"):
            runtime_images.pinned_model(model, selected, pins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
