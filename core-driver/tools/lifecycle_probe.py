#!/usr/bin/env python3
"""Lifecycle probe: a Zenoh peer that drives + verifies the service-lifecycle contract a core exposes
(docs/LIFECYCLE.md) -- the consumer side of what the dashboard / orchestrator does.

Usage: lifecycle_probe.py [--listen tcp/127.0.0.1:7447] [--connect tcp/host:7447] [--timeout 60]
                          [--pattern 'fleet/*/svc/*/lifecycle'] --steps STEP [STEP ...]

Steps, run in order (the first liveliness PUT picks the key under test):
  wait-put            a liveliness token appears (presence)
  get[:STATE]         fetch the descriptor via the queryable; validate; optionally assert its state
  activate            change_state {"transition": "activate"} -> expect ok
  deactivate          change_state {"transition": "deactivate"} -> expect ok
  bad                 change_state {"transition": "reboot"} -> expect a refusal (ok=false), not an error
  wait-state:STATE    the /state publisher (or a descriptor fetch) shows STATE
  sleep:S             wait S seconds (let a session record)
  wait-delete         the token disappears (graceful stop)

With --listen the probe stands in for a router: the core's default connect endpoint (tcp/localhost:7447)
reaches it and no zenohd exists anywhere -- the router-less deployment shape under test.

Emits flushed, line-oriented markers for a test harness to grep:
  READY
  EVENT PUT <key>            EVENT DELETE <key>
  DESCRIPTOR <key> state=<s> ok    DESCRIPTOR <key> bad <reason>
  REPLY <transition> ok=<True|False> state=<s> [error=<...>]
  STATE <key> <state>        (from the /state publisher)
  STEP_OK <step>             STEP_FAIL <step> <reason>
  SUMMARY steps=<n> failed=<n>
Exits 0 iff every step passed within the timeout.
"""
import argparse
import json
import os
import sys
import threading
import time

import zenoh

REQUIRED = {"schema_version": int, "service": str, "instance": str, "state": str, "transitions": list}

_T0 = time.monotonic()


def emit(*parts):
    print("[{:7.2f}s]".format(time.monotonic() - _T0), *parts, flush=True)


def validate(key, payload):
    try:
        d = json.loads(payload)
    except Exception as e:   # noqa: BLE001
        return None, "not-json:{}".format(e)
    if not isinstance(d, dict):
        return None, "not-object"
    for field, typ in REQUIRED.items():
        if field not in d:
            return None, "missing:{}".format(field)
        if not isinstance(d[field], typ):
            return None, "badtype:{}".format(field)
    inst = key.split("/")[3] if key.count("/") >= 4 else None
    if inst and d["instance"] != inst:
        return None, "instance-mismatch:{}!={}".format(d["instance"], inst)
    return d, "ok"


class Probe:
    def __init__(self, session, pattern, timeout):
        self.session = session
        self.timeout = timeout
        self.key = None
        self.alive = set()
        self.states = {}
        self.cv = threading.Condition()
        self.live_sub = session.liveliness().declare_subscriber(pattern, self._on_liveliness, history=True)
        self.state_sub = session.declare_subscriber(pattern + "/state", self._on_state)

    def _on_liveliness(self, sample):
        key = str(sample.key_expr)
        with self.cv:
            if sample.kind == zenoh.SampleKind.PUT:
                self.alive.add(key)
                if self.key is None:
                    self.key = key
                emit("EVENT", "PUT", key)
            else:
                self.alive.discard(key)
                emit("EVENT", "DELETE", key)
            self.cv.notify_all()

    def _on_state(self, sample):
        key = str(sample.key_expr)                                   # .../lifecycle/state
        base = key[:-len("/state")] if key.endswith("/state") else key   # the lifecycle key it belongs to
        try:
            pb = sample.payload
            d = json.loads(pb.to_bytes() if hasattr(pb, "to_bytes") else bytes(pb))
            state = d.get("state")
        except Exception as e:   # noqa: BLE001
            emit("STATE", key, "unparseable:{}".format(e))
            return
        with self.cv:
            self.states[base] = state
            emit("STATE", key, state)
            self.cv.notify_all()

    def _wait(self, pred, what):
        deadline = time.monotonic() + self.timeout
        with self.cv:
            while not pred():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False, "timeout waiting for {}".format(what)
                self.cv.wait(timeout=min(left, 1.0))
        return True, "ok"

    def _get(self, key, payload=None):
        kw = {"timeout": 20.0}
        if payload is not None:
            kw["payload"] = payload
            kw["encoding"] = zenoh.Encoding.APPLICATION_JSON
        for reply in self.session.get(key, **kw):
            if reply.ok:
                pb = reply.ok.payload
                return pb.to_bytes() if hasattr(pb, "to_bytes") else bytes(pb)
            return None
        return None

    # ---- steps ---------------------------------------------------------------
    def wait_put(self):
        return self._wait(lambda: self.key is not None and self.key in self.alive, "a liveliness PUT")

    def wait_delete(self):
        return self._wait(lambda: self.key is not None and self.key not in self.alive, "a liveliness DELETE")

    def get(self, want_state=None):
        payload = self._get(self.key)
        if payload is None:
            emit("DESCRIPTOR", self.key, "bad", "no-reply")
            return False, "no descriptor reply"
        d, reason = validate(self.key, payload)
        if d is None:
            emit("DESCRIPTOR", self.key, "bad", reason)
            return False, reason
        emit("DESCRIPTOR", self.key, "state={}".format(d["state"]), "ok")
        if want_state and d["state"] != want_state:
            return False, "state {} != {}".format(d["state"], want_state)
        return True, "ok"

    def change(self, transition, expect_ok=True):
        payload = self._get(self.key + "/change_state", json.dumps({"transition": transition}).encode())
        if payload is None:
            emit("REPLY", transition, "no-reply")
            return False, "no change_state reply"
        try:
            r = json.loads(payload)
        except Exception as e:   # noqa: BLE001
            return False, "reply not JSON: {}".format(e)
        emit("REPLY", transition, "ok={}".format(r.get("ok")), "state={}".format(r.get("state")),
             *(["error={}".format(r["error"])] if r.get("error") else []))
        if bool(r.get("ok")) != expect_ok:
            return False, "expected ok={} got {}".format(expect_ok, r.get("ok"))
        if "descriptor" not in r:
            return False, "reply carries no descriptor"
        return True, "ok"

    def wait_state(self, state):
        ok, why = self._wait(lambda: self.states.get(self.key) == state, "state {}".format(state))
        if ok:
            return True, "ok"
        return self.get(state)   # fall back to the queryable: a publication may predate our link


def run_steps(probe, steps):
    failed = 0
    for step in steps:
        name, _, arg = step.partition(":")
        if name == "wait-put":
            ok, why = probe.wait_put()
        elif name == "wait-delete":
            ok, why = probe.wait_delete()
        elif name == "get":
            ok, why = probe.get(arg or None)
        elif name in ("activate", "deactivate"):
            ok, why = probe.change(name)
        elif name == "bad":
            ok, why = probe.change("reboot", expect_ok=False)
        elif name == "wait-state":
            ok, why = probe.wait_state(arg)
        elif name == "sleep":
            time.sleep(float(arg))
            ok, why = True, "ok"
        else:
            ok, why = False, "unknown step"
        emit("STEP_OK" if ok else "STEP_FAIL", step, *([] if ok else [why]))
        if not ok:
            failed += 1
            break
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default="fleet/*/svc/*/lifecycle")
    ap.add_argument("--listen", default="")
    ap.add_argument("--connect", default=os.environ.get("ZENOH_CONNECT", ""))
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--steps", nargs="+", required=True)
    args = ap.parse_args()

    conf = zenoh.Config()
    conf.insert_json5("mode", '"peer"')
    if args.listen:
        conf.insert_json5("listen/endpoints", json.dumps([e.strip() for e in args.listen.split(",") if e.strip()]))
    if args.connect:
        conf.insert_json5("connect/endpoints", json.dumps([e.strip() for e in args.connect.split(",") if e.strip()]))
        conf.insert_json5("connect/timeout_ms", "2000")
        conf.insert_json5("connect/exit_on_failure", "false")
    session = zenoh.open(conf)
    probe = Probe(session, args.pattern, args.timeout)
    emit("READY")
    try:
        failed = run_steps(probe, args.steps)
    finally:
        emit("SUMMARY", "steps={}".format(len(args.steps)), "failed={}".format(failed))
        for sub in (probe.live_sub, probe.state_sub):
            try:
                sub.undeclare()
            except Exception:   # noqa: BLE001
                pass
        session.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
