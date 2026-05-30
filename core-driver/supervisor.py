"""Per-sensor supervisor: spawn the core driver + enabled plugins as child processes
and manage their lifecycle. This is the entrypoint for a single-sensor container.

Run under `docker run --init` (or compose `init: true`) so an init reaps orphaned
grandchildren; this supervisor manages its own direct children: it forwards shutdown
signals, monitors exits, restarts crashed plugins (with backoff), and on a core exit
or SIGTERM/SIGINT tears the whole sensor down cleanly (SIGINT first, so the core
finalizes its recording).

Spawns, from the config:
  - core:    python3 main.py -c <config>
  - plugins: each enabled entry in `plugins:`, resolved to a command either by a
             built-in launcher (by `name`) or an explicit `command:`.

Only plugins whose runtime is present in THIS image can be spawned here. Heavy plugins
with their own runtime (e.g. the ROS2 bridge) typically run as sibling containers
(see docker-compose.yml) sharing the shm transport.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

from gige_driver.config import load_config

log = logging.getLogger("supervisor")

RESTART_BACKOFF_S = 2.0
RESTART_MAX = 5          # give up after this many rapid restarts ...
RESTART_RESET_S = 30.0   # ... unless the plugin stayed up at least this long
STOP_GRACE_S = 10.0
STARTUP_STAGGER_S = 2.0  # let the core bring up its endpoints before plugins attach


def _ros2_bridge_command(params: dict) -> list:
    cmd = ["ros2", "run", "gige_ros2_bridge", "gige_ros2_bridge", "--ros-args"]
    for k, v in params.items():
        cmd += ["-p", f"{k}:={v}"]
    return cmd


# built-in launchers keyed by plugin name (extend as plugins are added)
_LAUNCHERS = {
    "ros2-bridge": _ros2_bridge_command,
}


class Service:
    def __init__(self, name: str, cmd: list, critical: bool, restart: bool):
        self.name = name
        self.cmd = cmd
        self.critical = critical    # a critical service's exit tears down the sensor (the core)
        self.restart = restart
        self.proc: Optional[subprocess.Popen] = None
        self.restarts = 0
        self.started_at = 0.0

    def spawn(self) -> None:
        log.info("spawn %s: %s", self.name, " ".join(self.cmd))
        self.proc = subprocess.Popen(self.cmd)
        self.started_at = time.monotonic()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Supervisor:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.services: list[Service] = []
        self._stopping = False

    def _build_services(self) -> None:
        self.services.append(Service(
            "core", [sys.executable, "main.py", "-c", self.config_path],
            critical=True, restart=False))
        for p in self.cfg.plugins:
            if not p.enabled:
                continue
            cmd = self._resolve(p)
            if cmd:
                self.services.append(Service(p.name, cmd, critical=False, restart=p.restart))
            else:
                log.warning("plugin %r: no built-in launcher and no `command:`; skipping", p.name)

    def _resolve(self, plugin) -> Optional[list]:
        if plugin.name in _LAUNCHERS:
            params = dict(plugin.params)
            # convenience: point the bridge at the core's endpoint unless overridden
            params.setdefault("socket_path", self.cfg.transport.plugin_endpoint.socket_path)
            return _LAUNCHERS[plugin.name](params)
        if isinstance(plugin.command, list) and plugin.command:
            return [str(a) for a in plugin.command]
        return None

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self._build_services()
        for s in self.services:
            if self._stopping:
                break
            s.spawn()
            if s.critical:
                time.sleep(STARTUP_STAGGER_S)  # core first, then plugins attach to its endpoint
        log.info("supervising %d service(s)", len(self.services))
        self._monitor()

    def _on_signal(self, signum, _frame) -> None:
        log.info("signal %s received; stopping sensor", signal.Signals(signum).name)
        self._stopping = True

    def _monitor(self) -> None:
        while not self._stopping:
            for s in self.services:
                if s.proc is None or s.alive():
                    continue
                rc = s.proc.returncode
                if s.critical:
                    log.error("core exited (rc=%s); tearing down sensor", rc)
                    self._teardown()
                    return
                self._handle_plugin_exit(s, rc)
            time.sleep(0.5)
        self._teardown()

    def _handle_plugin_exit(self, s: Service, rc) -> None:
        if time.monotonic() - s.started_at >= RESTART_RESET_S:
            s.restarts = 0  # ran fine for a while -> reset the rapid-restart counter
        if not s.restart:
            log.warning("plugin %s exited (rc=%s); restart disabled", s.name, rc)
            s.proc = None
            return
        if s.restarts >= RESTART_MAX:
            log.error("plugin %s crashed %d times; giving up", s.name, s.restarts)
            s.proc = None
            return
        s.restarts += 1
        log.warning("plugin %s exited (rc=%s); restarting in %.1fs (%d/%d)",
                    s.name, rc, RESTART_BACKOFF_S, s.restarts, RESTART_MAX)
        time.sleep(RESTART_BACKOFF_S)
        if self._stopping:
            return
        s.spawn()

    def _teardown(self) -> None:
        self._stopping = True
        for s in self.services:           # SIGINT = clean stop (core finalizes its recording)
            if s.alive():
                log.info("stopping %s", s.name)
                try:
                    s.proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + STOP_GRACE_S
        for s in self.services:
            if s.proc is None:
                continue
            try:
                s.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("%s did not exit in time; killing", s.name)
                s.proc.kill()
        log.info("sensor stopped")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-sensor process supervisor")
    ap.add_argument("-c", "--config", default=os.environ.get("GIGE_CONFIG", "config/camera.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    Supervisor(args.config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
