"""Tests for the lifecycle policy layer (cam_driver.lifecycle): the inactive/active state machine in
front of the pipeline's recording sessions -- legal transitions, idempotency, refusals, the state
descriptor, and the remembered-state file a crash restart resumes from.

Pure Python against a stub pipeline (activate / deactivate / get_state); no GStreamer, no zenoh.

Run: python3 core-driver/tests/test_lifecycle.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.lifecycle import ACTIVE, INACTIVE, Lifecycle, sanitize_run_id, transitions_from  # noqa: E402


class _StubPipe:
    """The mechanism, faked: flips its own state on activate/deactivate and records the calls."""

    def __init__(self, *, activate_fails=False, activate_raises=False, close_error=None):
        self.state = INACTIVE
        self.session = None
        self.calls = []
        self.on_session_ended = None
        self._activate_fails = activate_fails
        self._activate_raises = activate_raises
        self._close_error = close_error

    def activate(self, run_id=None):
        self.calls.append(("activate", run_id))
        if self._activate_raises:
            raise RuntimeError("no encoder")
        if self._activate_fails:
            return {"ok": False, "error": "session start failed: nope", "state": INACTIVE}
        self.state = ACTIVE
        n = len([c for c in self.calls if c[0] == "activate"])
        self.session = {"index": n, "prefix": f"cam-{run_id or 'x'}", "frames": 0}
        return {"ok": True, "state": ACTIVE, "session": dict(self.session)}

    def deactivate(self, wait_eos=True):
        self.calls.append(("deactivate", wait_eos))
        s, self.session, self.state = self.session, None, INACTIVE
        r = {"ok": True, "state": INACTIVE,
             "session": {**(s or {}), "truncated": False, "error": self._close_error}}
        if self._close_error:
            r["error"] = self._close_error
        return r

    def get_state(self):
        return {"state": self.state, "session": dict(self.session) if self.session else None,
                "health": {"frames": 7, "stalled": False}, "encoder": "ffv1", "last_result": None}


def _lc(pipe=None, tmp=None, **kw):
    pipe = pipe or _StubPipe()
    args = dict(recording_enabled=True, initial_state=ACTIVE,
                state_file=os.path.join(tmp, "lifecycle.state") if tmp else "", resume=bool(tmp),
                instance="cam_test", segment_seconds=30)
    args.update(kw)
    return pipe, Lifecycle(pipe, **args)


def test_activate_then_deactivate():
    pipe, lc = _lc()
    r = lc.request("activate")
    assert r["ok"] and r["state"] == ACTIVE and pipe.calls == [("activate", None)]
    d = r["descriptor"]
    assert d["state"] == ACTIVE and d["transitions"] == ["deactivate"]
    assert d["recording"]["encoder"] == "ffv1" and d["recording"]["segment_seconds"] == 30
    r = lc.request("deactivate")
    assert r["ok"] and r["state"] == INACTIVE and r["session"]["truncated"] is False
    assert lc.descriptor()["transitions"] == ["activate"] and "recording" not in lc.descriptor()


def test_repeated_transition_is_an_idempotent_noop():
    # An orchestrator that re-sends its desired state must get agreement, not an error.
    pipe, lc = _lc()
    lc.request("activate")
    r = lc.request("activate")
    assert r["ok"] and r.get("noop") is True and len(pipe.calls) == 1
    lc.request("deactivate")
    r = lc.request("deactivate")
    assert r["ok"] and r.get("noop") and len(pipe.calls) == 2


def test_unknown_transition_is_refused_with_the_legal_list():
    pipe, lc = _lc()
    r = lc.request("reboot")
    assert not r["ok"] and "unknown transition" in r["error"] and pipe.calls == []
    assert r["descriptor"]["transitions"] == ["activate"]


def test_recording_disabled_refuses_activate_and_boots_inactive():
    pipe, lc = _lc(recording_enabled=False)
    r = lc.request("activate")
    assert not r["ok"] and r["error"] == "recording disabled by config" and pipe.calls == []
    assert lc.resolve_boot_state() == (INACTIVE, "derived")


def test_mechanism_failure_is_a_refusal_not_a_crash():
    pipe, lc = _lc(_StubPipe(activate_raises=True))
    r = lc.request("activate")
    assert not r["ok"] and "no encoder" in r["error"] and lc.state == INACTIVE
    assert "no encoder" in lc.last_error
    pipe, lc = _lc(_StubPipe(activate_fails=True))
    r = lc.request("activate")
    assert not r["ok"] and lc.state == INACTIVE and "nope" in lc.last_error


def test_run_id_is_sanitized_into_the_prefix():
    pipe, lc = _lc()
    lc.request("activate", {"run_id": "leg 3/../north"})
    assert pipe.calls == [("activate", "leg-3-..-north")]
    assert sanitize_run_id(None) is None and sanitize_run_id("") is None and sanitize_run_id("///") is None
    assert len(sanitize_run_id("x" * 200)) == 64


def test_finalize_trouble_is_surfaced_as_last_error():
    pipe, lc = _lc(_StubPipe(close_error="disk full"))
    lc.request("activate")
    r = lc.request("deactivate")
    assert r["ok"] and r["state"] == INACTIVE and r["error"] == "disk full"
    assert lc.last_error == "disk full"


def test_remember_forget_recall():
    with tempfile.TemporaryDirectory() as tmp:
        pipe, lc = _lc(tmp=tmp)
        path = os.path.join(tmp, "lifecycle.state")
        assert lc.recall() is None
        lc.request("activate")
        assert open(path).read().strip() == ACTIVE
        assert lc.recall() == ACTIVE
        lc.request("deactivate")
        assert open(path).read().strip() == INACTIVE
        lc.forget()
        assert not os.path.exists(path) and lc.recall() is None
        lc.forget()                                  # idempotent
        with open(path, "w") as f:
            f.write("garbage\n")
        assert lc.recall() is None


def test_resume_off_touches_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        pipe, lc = _lc(tmp=tmp, resume=False)
        lc.request("activate")
        lc.forget()
        assert os.listdir(tmp) == []


def test_boot_resolution_order():
    with tempfile.TemporaryDirectory() as tmp:
        pipe, lc = _lc(tmp=tmp, initial_state=INACTIVE)
        assert lc.resolve_boot_state() == (INACTIVE, "config")
        with open(os.path.join(tmp, "lifecycle.state"), "w") as f:
            f.write("active\n")
        assert lc.resolve_boot_state() == (ACTIVE, "resumed"), "a crash restart resumes the last command"
        pipe2, lc2 = _lc(tmp=tmp, recording_enabled=False)
        assert lc2.resolve_boot_state() == (INACTIVE, "derived"), "disabled recording beats a remembered active"


def test_boot_active_activates_and_reports_failure():
    pipe, lc = _lc()
    assert lc.boot() is True and pipe.calls == [("activate", None)] and lc.boot_reason == "config"
    pipe, lc = _lc(initial_state=INACTIVE)
    assert lc.boot() is True and pipe.calls == [] and lc.state == INACTIVE
    pipe, lc = _lc(_StubPipe(activate_fails=True))
    assert lc.boot() is False, "an unopenable boot session is fatal, as an unbuildable recorder was"


def test_uncommanded_end_is_remembered_as_inactive():
    # The pipeline closed the session on its own (recorder ERROR): the honest state is inactive, and
    # THAT is what a crash restart should find -- not a remembered `active` walking back into the fault.
    with tempfile.TemporaryDirectory() as tmp:
        pipe, lc = _lc(tmp=tmp)
        seen = []
        lc.add_observer(seen.append)
        lc.request("activate")
        assert pipe.on_session_ended is not None
        pipe.state, pipe.session = INACTIVE, None
        pipe.on_session_ended({"ok": True, "error": "disk full", "session": {}})
        assert lc.last_error == "disk full"
        assert open(os.path.join(tmp, "lifecycle.state")).read().strip() == INACTIVE
        assert seen[-1]["state"] == INACTIVE and seen[-1]["last_error"] == "disk full"


def test_observers_see_every_transition_and_never_break_one():
    pipe, lc = _lc()
    seen = []

    def bad(_d):
        raise RuntimeError("observer bug")

    lc.add_observer(bad)
    lc.add_observer(seen.append)
    assert lc.request("activate")["ok"]
    assert lc.request("deactivate")["ok"]
    assert [d["state"] for d in seen] == [ACTIVE, INACTIVE]


def test_descriptor_required_fields():
    pipe, lc = _lc(initial_state=INACTIVE)
    lc.boot()
    d = lc.descriptor()
    for k in ("schema_version", "service", "instance", "state", "transitions"):
        assert k in d, k
    assert d["service"] == "camera-service" and d["instance"] == "cam_test" and d["schema_version"] == 1
    assert d["boot_reason"] == "config" and d["health"]["frames"] == 7 and d["recording_enabled"] is True
    assert transitions_from(ACTIVE) == ["deactivate"] and transitions_from(INACTIVE) == ["activate"]


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
