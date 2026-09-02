"""Tests for the zenoh control plane adapter (cam_driver.control_zenoh): request parsing, the
change_state round-trip through the lifecycle on the (injected) main-loop dispatcher, the descriptor
query, state publication, and the fail-safe open/close -- all against fake session / query objects.

No zenoh binding and no gi are needed: both are injected, so this runs (and must not skip) in CI.

Run: python3 core-driver/tests/test_control_zenoh.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.control_zenoh import (ZenohControl, encode_json, lifecycle_key,  # noqa: E402
                                      parse_change_state, zenoh_connect_endpoints)

KEY = lifecycle_key("veh", "cam_test")


class _Lifecycle:
    def __init__(self):
        self.state = "inactive"
        self.requests = []
        self.observers = []
        self.raise_on = None

    def add_observer(self, fn):
        self.observers.append(fn)

    def descriptor(self):
        return {"schema_version": 1, "service": "camera-service", "instance": "cam_test",
                "state": self.state,
                "transitions": ["activate"] if self.state == "inactive" else ["deactivate"]}

    def request(self, transition, params=None):
        self.requests.append((transition, dict(params or {})))
        if transition == self.raise_on:
            raise RuntimeError("mechanism blew up")
        if transition == "activate":
            self.state = "active"
        elif transition == "deactivate":
            self.state = "inactive"
        else:
            return {"ok": False, "error": "unknown transition", "state": self.state,
                    "descriptor": self.descriptor()}
        d = self.descriptor()
        for fn in self.observers:
            fn(d)
        return {"ok": True, "state": self.state, "descriptor": d}


class _Declared:
    def __init__(self, key):
        self.key = key
        self.undeclared = 0

    def undeclare(self):
        self.undeclared += 1


class _Publisher(_Declared):
    def __init__(self, key):
        super().__init__(key)
        self.puts = []

    def put(self, payload, encoding=None):
        self.puts.append(json.loads(payload))


class _Liveliness:
    def __init__(self, session):
        self._s = session

    def declare_token(self, key):
        t = _Declared(key)
        self._s.tokens.append(t)
        return t


class _Session:
    def __init__(self, endpoints):
        self.endpoints = list(endpoints)
        self.queryables = {}
        self.publisher = None
        self.tokens = []
        self.closed = 0

    def declare_queryable(self, key, cb):
        q = _Declared(key)
        self.queryables[key] = (cb, q)
        return q

    def declare_publisher(self, key):
        self.publisher = _Publisher(key)
        return self.publisher

    def liveliness(self):
        return _Liveliness(self)

    def close(self):
        self.closed += 1


class _Payload:
    def __init__(self, b):
        self._b = b

    def to_bytes(self):
        return self._b


class _Query:
    def __init__(self, key, payload=None, parameters=""):
        self.key_expr = key
        self.payload = _Payload(payload) if payload is not None else None
        self.parameters = parameters
        self.replies = []

    def reply(self, key, payload, encoding=None):
        self.replies.append((str(key), json.loads(payload)))


def _control(connect=("tcp/localhost:7447",), enabled=True):
    lc = _Lifecycle()
    sessions, dispatched, fail = [], [], {"open": False}

    def factory(endpoints):
        if fail["open"]:
            raise RuntimeError("router unreachable")
        s = _Session(endpoints)
        sessions.append(s)
        return s

    ctl = ZenohControl(lc, KEY, connect=connect, enabled=enabled,
                       dispatch=lambda fn, *a: dispatched.append((fn, a)), session_factory=factory)
    return ctl, lc, sessions, dispatched, fail


def _run_dispatched(dispatched):
    while dispatched:
        fn, args = dispatched.pop(0)
        assert fn(*args) is False, "a dispatched handler must be a one-shot idle callback"


# ---- pure helpers ------------------------------------------------------------
def test_parse_change_state_payload_and_parameters():
    assert parse_change_state(b'{"transition": "activate"}') == ({"transition": "activate"}, None)
    req, err = parse_change_state(b'{"transition": "activate", "run_id": 7, "x": 1}')
    assert err is None and req == {"transition": "activate", "run_id": "7"}
    assert parse_change_state(None, "transition=deactivate") == ({"transition": "deactivate"}, None)
    assert parse_change_state(b"", "transition=activate;run_id=leg3") == \
        ({"transition": "activate", "run_id": "leg3"}, None)
    assert parse_change_state(None, "transition=activate&run_id=leg3")[0]["run_id"] == "leg3"


def test_parse_change_state_refusals():
    assert parse_change_state(b"{not json")[1].startswith("request is not JSON")
    assert parse_change_state(b"[1, 2]")[1] == "request must be a JSON object"
    assert parse_change_state(b"{}")[1] == "missing 'transition'"
    assert parse_change_state(None, "")[1] == "missing 'transition'"
    assert parse_change_state(b'{"transition": 5}')[1] == "missing 'transition'"
    assert parse_change_state(b'{"transition": "activate", "run_id": [1]}')[1] == "'run_id' must be a string"


def test_connect_endpoints_precedence():
    saved = os.environ.pop("ZENOH_CONNECT", None)
    try:
        assert zenoh_connect_endpoints("tcp/a:1, tcp/b:2") == ["tcp/a:1", "tcp/b:2"], "YAML wins"
        assert zenoh_connect_endpoints("") == [], "explicitly empty = scout only"
        assert zenoh_connect_endpoints(None) == ["tcp/localhost:7447"], "unset everywhere = the local zenohd"
        os.environ["ZENOH_CONNECT"] = "tcp/router:7447"
        assert zenoh_connect_endpoints(None) == ["tcp/router:7447"]
        assert zenoh_connect_endpoints("tcp/yaml:1") == ["tcp/yaml:1"]
        os.environ["ZENOH_CONNECT"] = ""
        assert zenoh_connect_endpoints(None) == []
    finally:
        if saved is None:
            os.environ.pop("ZENOH_CONNECT", None)
        else:
            os.environ["ZENOH_CONNECT"] = saved


def test_key_and_encoding():
    assert KEY == "fleet/veh/svc/cam_test/lifecycle"
    assert json.loads(encode_json({"a": 1, "b": [1, 2]})) == {"a": 1, "b": [1, 2]}


# ---- the adapter ---------------------------------------------------------------
def test_advertise_declares_everything_token_last_and_publishes_the_initial_state():
    ctl, lc, sessions, dispatched, fail = _control()
    assert ctl.advertise() is True and ctl.active
    s = sessions[0]
    assert s.endpoints == ["tcp/localhost:7447"]
    assert set(s.queryables) == {KEY, KEY + "/change_state"}
    assert s.publisher.key == KEY + "/state" and s.tokens[0].key == KEY
    assert s.publisher.puts == [lc.descriptor()], "consumers with a history-backed subscriber see the boot state"
    assert ctl.advertise() is True and len(sessions) == 1, "idempotent"


def test_disabled_control_plane_opens_nothing():
    ctl, lc, sessions, dispatched, fail = _control(enabled=False)
    assert ctl.advertise() is False and sessions == [] and not ctl.active


def test_open_failure_is_contained_and_retryable():
    ctl, lc, sessions, dispatched, fail = _control()
    fail["open"] = True
    assert ctl.advertise() is False and not ctl.active and sessions == []
    fail["open"] = False
    assert ctl.advertise() is True and ctl.active, "the next attempt (the retry timer) succeeds cleanly"


def test_state_query_replies_the_descriptor_on_the_zenoh_thread():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    cb, _q = sessions[0].queryables[KEY]
    q = _Query("fleet/*/svc/*/lifecycle")
    cb(q)
    assert dispatched == [], "no main-loop hop for a read"
    assert q.replies == [("fleet/*/svc/*/lifecycle", lc.descriptor())]


def test_change_state_round_trip_runs_on_the_main_loop_and_publishes():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    cb, _q = sessions[0].queryables[KEY + "/change_state"]
    q = _Query(KEY + "/change_state", payload=b'{"transition": "activate", "run_id": "leg3"}')
    cb(q)
    assert q.replies == [] and len(dispatched) == 1, "the transition waits for the main loop"
    _run_dispatched(dispatched)
    assert lc.requests == [("activate", {"transition": "activate", "run_id": "leg3"})]
    key, reply = q.replies[0]
    assert reply["ok"] is True and reply["state"] == "active" and reply["descriptor"]["state"] == "active"
    assert sessions[0].publisher.puts[-1]["state"] == "active", "the observer published the transition"

    q2 = _Query(KEY + "/change_state", parameters="transition=deactivate")
    cb(q2)
    _run_dispatched(dispatched)
    assert lc.requests[-1] == ("deactivate", {"transition": "deactivate"})
    assert q2.replies[0][1]["ok"] is True and q2.replies[0][1]["state"] == "inactive"


def test_bad_request_is_refused_without_touching_the_lifecycle():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    cb, _q = sessions[0].queryables[KEY + "/change_state"]
    q = _Query(KEY + "/change_state", payload=b"nope")
    cb(q)
    _run_dispatched(dispatched)
    reply = q.replies[0][1]
    assert reply["ok"] is False and "not JSON" in reply["error"] and reply["state"] == "inactive"
    assert lc.requests == []
    q = _Query(KEY + "/change_state", payload=b'{"transition": "reboot"}')
    cb(q)
    _run_dispatched(dispatched)
    assert q.replies[0][1] == {"ok": False, "error": "unknown transition", "state": "inactive",
                               "descriptor": lc.descriptor()}


def test_mechanism_exception_becomes_an_error_reply():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    lc.raise_on = "activate"
    cb, _q = sessions[0].queryables[KEY + "/change_state"]
    q = _Query(KEY + "/change_state", payload=b'{"transition": "activate"}')
    cb(q)
    _run_dispatched(dispatched)
    assert q.replies[0][1]["ok"] is False and "internal error" in q.replies[0][1]["error"]


def test_dispatch_failure_answers_immediately():
    lc = _Lifecycle()

    def broken_dispatch(_fn, *_a):
        raise RuntimeError("no loop")

    ctl = ZenohControl(lc, KEY, dispatch=broken_dispatch, session_factory=lambda eps: _Session(eps))
    ctl.advertise()
    cb, _q = ctl._session.queryables[KEY + "/change_state"]
    q = _Query(KEY + "/change_state", payload=b'{"transition": "activate"}')
    cb(q)
    assert q.replies[0][1]["ok"] is False and "dispatch failed" in q.replies[0][1]["error"]


def test_signal_driven_transitions_are_published_too():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    lc.request("activate")              # what SIGUSR1 does, bypassing zenoh entirely
    assert sessions[0].publisher.puts[-1]["state"] == "active"


def test_close_withdraws_everything_and_publishing_stops():
    ctl, lc, sessions, dispatched, fail = _control()
    ctl.advertise()
    s = sessions[0]
    ctl.close()
    assert not ctl.active and s.closed == 1
    assert s.tokens[0].undeclared == 1 and s.publisher.undeclared == 1
    assert all(q.undeclared == 1 for _cb, q in s.queryables.values())
    lc.request("activate")
    assert len(s.publisher.puts) == 1, "no publish after close"
    assert ctl.advertise() is False or True   # never raises after close


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
