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

from cam_driver.config import load_config

log = logging.getLogger("supervisor")

RESTART_BACKOFF_S = 2.0
RESTART_MAX = 5          # give up after this many rapid restarts ...
RESTART_RESET_S = 30.0   # ... unless the plugin stayed up at least this long
STARTUP_STAGGER_S = 2.0  # let the core bring up its endpoints before plugins attach

# Stop budgets, PER SERVICE CLASS. The core's clean-stop chain is a stack of timers -- up to 5 s for
# the EOS drain (pipeline._force_quit), then the NULL transition, then the device release (GigE
# control privilege), then up to 5 s joining the sidecar writer -- so a single 10 s budget shared
# across every child could not cover it, and `kill()` landed mid-finalization: a truncated .mkv AND
# an unreleased control privilege the camera then holds until its heartbeat timeout. That is the
# exact incident docker-compose.yml's `stop_grace_period: 25s` was raised for, and it was
# unreachable because THIS deadline is the tighter one.
#
# CORE_STOP_GRACE_S must stay comfortably inside that 25 s -- Docker SIGKILLs the whole container
# there, and the supervisor still needs time to reap and log afterwards. Keep the two in step.
CORE_STOP_GRACE_S = 20.0
PLUGIN_STOP_GRACE_S = 10.0


def _ros2_bridge_command(params: dict) -> list:
    cmd = ["ros2", "run", "cam_ros2_bridge", "cam_ros2_bridge", "--ros-args"]
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
        self._exit_rc = 0        # the sensor's exit status; a dead core propagates its own

    def _build_services(self) -> None:
        self.services.append(Service(
            "core", [sys.executable, "main.py", "-c", self.config_path],
            critical=True, restart=False))
        for p in self.cfg.plugins:
            if not p.enabled:
                continue
            if p.isolation != "process":
                # heavy plugin with its own image — a compose sibling, not ours to spawn
                log.info("plugin %r: isolation=%s -> managed by compose, skipping", p.name, p.isolation)
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

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self._build_services()
        for s in self.services:
            if self._stopping:
                break
            try:
                s.spawn()
            except OSError as e:
                # A missing binary / typo'd `command:` must not kill PID 1 -- the orphaned core
                # would be SIGKILLed mid-recording with no teardown. Critical (the core) means the
                # sensor can't run: tear down whatever already spawned and exit non-zero. A plugin
                # just gets skipped; the sensor runs without it.
                if s.critical:
                    log.error("cannot spawn %s: %s; tearing down sensor", s.name, e)
                    self._teardown()
                    return 1
                log.error("cannot spawn plugin %s: %s; running without it", s.name, e)
                continue
            if s.critical:
                time.sleep(STARTUP_STAGGER_S)  # core first, then plugins attach to its endpoint
        log.info("supervising %d service(s)", len(self.services))
        self._monitor()
        return self._exit_rc

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
                    # Propagate the core's status instead of reporting a clean exit for a hard
                    # failure: rc=2 is a config / pipeline-build error and rc=1 a bus ERROR, and with
                    # `restart: unless-stopped` a swallowed 2 turned a misconfiguration into an
                    # unbounded restart loop rendered as `Exited (0)` in `docker ps` / `rig status`.
                    # `rc or 1` because an unexpected clean core exit still means the sensor is dead.
                    # Only reachable inside `while not self._stopping`, so an operator `docker stop`
                    # can't produce a false non-zero here.
                    self._exit_rc = rc or 1
                    log.error("core exited (rc=%s); tearing down sensor (exiting %d)", rc, self._exit_rc)
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
        try:
            s.spawn()
        except OSError as e:
            # Leave the dead proc in place: the monitor re-detects the exit and retries with the
            # same backoff until RESTART_MAX. Refresh started_at so the "ran fine for a while"
            # counter reset can't turn a vanished binary into an infinite retry loop.
            s.started_at = time.monotonic()
            log.error("plugin %s respawn failed: %s (%d/%d)", s.name, e, s.restarts, RESTART_MAX)

    @staticmethod
    def _stop_grace(s: Service) -> float:
        return CORE_STOP_GRACE_S if s.critical else PLUGIN_STOP_GRACE_S

    def _teardown(self) -> None:
        self._stopping = True
        for s in self.services:           # SIGINT = clean stop (core finalizes its recording)
            if s.alive():
                log.info("stopping %s", s.name)
                try:
                    s.proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
        # Every child was signalled at the same instant, so each budget is absolute FROM HERE and
        # they run concurrently -- a single shared countdown let a slow plugin spend the core's
        # budget before the core was even waited on. Shortest-first so a short wait never sits
        # behind a long one (the core is last, and gets its full grace regardless).
        signalled_at = time.monotonic()
        for s in sorted(self.services, key=self._stop_grace):
            if s.proc is None:
                continue
            grace = self._stop_grace(s)
            try:
                s.proc.wait(timeout=max(0.0, signalled_at + grace - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("%s did not exit within %.0fs; killing (its recording may be truncated)",
                            s.name, grace)
                s.proc.kill()
        log.info("sensor stopped")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-sensor process supervisor")
    ap.add_argument("-c", "--config", default=os.environ.get("CAM_CONFIG", "config/camera.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        sup = Supervisor(args.config)
    except (ValueError, OSError) as e:
        # Mirror main.py: a config typo / unreadable file fails fast + legibly, naming the
        # offending field, instead of a raw traceback from PID 1.
        log.error("config error: %s", e)
        return 2
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
