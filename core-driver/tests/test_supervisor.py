"""Tests for the supervisor's EXIT-STATUS and STOP-BUDGET semantics.

tools/supervisor_test.sh covers spawn/manage/clean-teardown in a container. What it can't see is the
status the sensor reports for a HARD failure: run() returned a hardcoded 0 after the core died, so
rc=2 (config / pipeline-build error) and rc=1 (bus ERROR) were both erased. Under
`restart: unless-stopped` that turned a misconfiguration into an unbounded restart loop rendered as
`Exited (0)` -- the opposite of what an operator reading `rig status` needs.

The stop-budget half is a cross-file invariant: the supervisor's grace has to fit inside the compose
`stop_grace_period`, and the whole defect was that the two disagreed silently.

No GStreamer here -- supervisor.py is stdlib + config. Run: python3 core-driver/tests/test_supervisor.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from supervisor import (CORE_STOP_GRACE_S, PLUGIN_STOP_GRACE_S, Service, Supervisor)

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")


class _FakeProc:
    """Just enough subprocess.Popen for _monitor/_teardown: an already-exited child."""

    def __init__(self, rc):
        self.returncode = rc
        self._rc = rc
        self.killed = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc

    def send_signal(self, _sig):
        pass

    def kill(self):
        self.killed = True


def _supervisor(services):
    """A Supervisor without __init__ (which would need a real config file on disk)."""
    sup = Supervisor.__new__(Supervisor)
    sup.services = services
    sup._stopping = False
    sup._exit_rc = 0
    return sup


def _dead_core(rc):
    s = Service("core", ["core"], critical=True, restart=False)
    s.proc = _FakeProc(rc)
    return s


def test_dead_core_propagates_its_own_exit_code():
    for rc in (1, 2):
        sup = _supervisor([_dead_core(rc)])
        sup._monitor()
        assert sup._exit_rc == rc, f"core rc={rc} must reach the container, got {sup._exit_rc}"


def test_run_returns_the_propagated_code_not_zero():
    sup = _supervisor([_dead_core(2)])
    sup._monitor()
    assert sup._exit_rc != 0


def test_unexpectedly_clean_core_exit_is_still_a_failure():
    # The core returning 0 on its own means it stopped without being asked to -- the sensor is dead
    # either way, and reporting success would tell compose/rig that nothing is wrong.
    sup = _supervisor([_dead_core(0)])
    sup._monitor()
    assert sup._exit_rc == 1


def test_operator_stop_is_not_reported_as_a_failure():
    # _monitor's critical branch is only reachable inside `while not self._stopping`, so a SIGTERM
    # that arrived first must leave the status clean.
    sup = _supervisor([_dead_core(2)])
    sup._stopping = True
    sup._monitor()
    assert sup._exit_rc == 0


def test_core_gets_a_longer_stop_budget_than_plugins():
    core = Service("core", [], critical=True, restart=False)
    plugin = Service("p", [], critical=False, restart=True)
    assert Supervisor._stop_grace(core) == CORE_STOP_GRACE_S
    assert Supervisor._stop_grace(plugin) == PLUGIN_STOP_GRACE_S
    # The core's clean-stop chain is ~5s EOS drain + NULL + device release + ~5s sidecar join, so a
    # budget at or below the plugin's could not cover it -- that is how kill() landed mid-finalize.
    assert CORE_STOP_GRACE_S > PLUGIN_STOP_GRACE_S
    assert CORE_STOP_GRACE_S >= 15.0


def test_core_stop_budget_fits_inside_the_compose_stop_grace_period():
    # A cross-FILE invariant, which is exactly why it drifted: Docker SIGKILLs the container at
    # stop_grace_period, so a supervisor budget at or beyond it can never be honoured. Keep headroom
    # for the supervisor to reap and log after the core is down.
    compose = os.path.join(_REPO, "docker-compose.yml")
    if not os.path.exists(compose):
        return   # repo root not reachable (dev container mounts core-driver/ alone); CI covers it
    with open(compose) as f:
        m = re.search(r"stop_grace_period:\s*(\d+)s", f.read())
    assert m, "docker-compose.yml no longer declares a stop_grace_period for the core"
    compose_s = int(m.group(1))
    assert CORE_STOP_GRACE_S < compose_s, (
        f"CORE_STOP_GRACE_S={CORE_STOP_GRACE_S} must stay inside compose's {compose_s}s, or Docker "
        "kills the container before the supervisor's own deadline can fire")


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
