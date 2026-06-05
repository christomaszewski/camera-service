#!/usr/bin/env python3
"""Discovery probe: a Zenoh client that verifies the fleet media-discovery contract a producer
advertises (docs/DISCOVERY.md). Streams liveliness add/remove events and validates the descriptor —
the consumer side of what the dashboard does.

Usage: discovery_probe.py [--pattern fleet/*/media/*] [--connect tcp/host:7447] [--timeout 90]

Emits flushed, line-oriented markers for a test harness to grep:
  READY
  EVENT PUT <key>        EVENT DELETE <key>
  DESCRIPTOR_OK <key>    DESCRIPTOR_BAD <key> <reason>
  SUMMARY put=<n> delete=<n> descriptor_ok=<0|1>
Exits 0 iff it observed >=1 PUT, a valid descriptor, and >=1 DELETE within the timeout.
"""
import argparse
import json
import os
import sys
import time

import zenoh

# Required descriptor fields + types (the cross-producer core of docs/DISCOVERY.md; optional fields
# like role/codec/width/height/fps/pixel_format/ros_topic/recording are not asserted here).
REQUIRED = {"schema_version": int, "id": str, "producer": str, "protocol": str,
            "signalling": str, "producer_id": str}


def emit(*parts):
    print(*parts, flush=True)


def validate(key, payload):
    try:
        d = json.loads(payload)
    except Exception as e:
        return False, "not-json:{}".format(e)
    if not isinstance(d, dict):
        return False, "not-object"
    for field, typ in REQUIRED.items():
        if field not in d:
            return False, "missing:{}".format(field)
        if not isinstance(d[field], typ):
            return False, "badtype:{}".format(field)
    sensor = key.rsplit("/", 1)[-1]
    if d["id"] != sensor:
        return False, "id-mismatch:{}!={}".format(d["id"], sensor)
    return True, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default="fleet/*/media/*")
    ap.add_argument("--connect", default=os.environ.get("ZENOH_CONNECT", ""))
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    conf = zenoh.Config()
    conf.insert_json5("mode", '"peer"')
    if args.connect:
        eps = [e.strip() for e in args.connect.split(",") if e.strip()]
        conf.insert_json5("connect/endpoints", json.dumps(eps))
    session = zenoh.open(conf)

    state = {"put": 0, "delete": 0, "ok": False}

    def fetch_descriptor(key):
        try:
            for reply in session.get(key):
                if reply.ok:
                    pb = reply.ok.payload
                    payload = pb.to_bytes() if hasattr(pb, "to_bytes") else bytes(pb)
                    good, reason = validate(key, payload)
                    if good:
                        state["ok"] = True
                        emit("DESCRIPTOR_OK", key)
                    else:
                        emit("DESCRIPTOR_BAD", key, reason)
                    return
            emit("DESCRIPTOR_BAD", key, "no-reply")
        except Exception as e:
            emit("DESCRIPTOR_BAD", key, "get-error:{}".format(e))

    def on_liveliness(sample):
        is_put = sample.kind == zenoh.SampleKind.PUT
        key = str(sample.key_expr)
        emit("EVENT", "PUT" if is_put else "DELETE", key)
        if is_put:
            state["put"] += 1
            fetch_descriptor(key)
        else:
            state["delete"] += 1

    sub = session.liveliness().declare_subscriber(args.pattern, on_liveliness, history=True)
    emit("READY")

    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            if state["put"] >= 1 and state["ok"] and state["delete"] >= 1:
                break
            time.sleep(0.2)
    finally:
        emit("SUMMARY", "put={}".format(state["put"]), "delete={}".format(state["delete"]),
             "descriptor_ok={}".format(int(state["ok"])))
        try:
            sub.undeclare()
        except Exception:
            pass
        session.close()

    return 0 if (state["put"] >= 1 and state["ok"] and state["delete"] >= 1) else 1


if __name__ == "__main__":
    sys.exit(main())
