#!/usr/bin/env python3
"""Instrumented, synthetic mono-camera combined-load qualification (Linux/GStreamer).

Run ``capture`` and ``ros-reader`` in separate containers with the same work directory
and Unix socket directory mounted at identical paths. Start the existing ROS bridge
against SOCKET_DIR/unixfd and topic /optimization_bench/image. The capture waits for
the reader's discovery handshake before starting. Run ``verify`` after both finish.

This is a correctness/soak tool, not an uninstrumented throughput benchmark. Every
captured and received image is hashed in full. Expected/received frames and 1 Hz RSS
samples are streamed to disk; only bounded recent metadata and histograms stay in
memory. Latency is service host-arrival capture stamp to local receipt, not sensor
exposure, network, browser, or display latency. Preview is a headless Gst fakesink.

Example (paths must also be visible to the ROS reader):
  combined_load_soak.py capture --work /reports/soak --socket-dir /soak-sockets
  combined_load_soak.py ros-reader --work /reports/soak
  combined_load_soak.py verify --work /reports/soak
Use a fresh work directory for each run; 45 seconds is useful for the initial smoke.
"""
import argparse
from array import array
from collections import OrderedDict, deque
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def rss_mb(pid="self"):
    return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20


class Histogram:
    """Fixed 1 ms bins, with overflow retained in the final bin (10 seconds)."""
    def __init__(self):
        self.bins = array("Q", [0]) * 10001
        self.count = 0
        self.total_ms = 0.0
        self.maximum_ms = 0.0

    def add(self, milliseconds):
        self.bins[min(10000, max(0, int(milliseconds)))] += 1
        self.count += 1
        self.total_ms += milliseconds
        self.maximum_ms = max(self.maximum_ms, milliseconds)

    def result(self):
        def quantile(q):
            target, count = max(1, int(self.count * q + .999999)), 0
            for index, value in enumerate(self.bins):
                count += value
                if count >= target:
                    return index + 1 if index < 10000 else ">=10000"
            return None
        return {"count": self.count, "mean_ms": self.total_ms / self.count if self.count else None,
                "p95_ms_upper_bound": quantile(.95), "p99_ms_upper_bound": quantile(.99),
                "max_ms": self.maximum_ms, "bin_width_ms": 1}


class Memory:
    def __init__(self, maximum, growth):
        self.maximum, self.growth = maximum, growth
        self.peak = 0.0
        self.initial = deque(maxlen=30)
        self.tail = deque(maxlen=30)

    def add(self, elapsed, value):
        self.peak = max(self.peak, value)
        # Ignore startup allocator/codec warmup, and retain a bounded early baseline.
        if elapsed >= 5 and len(self.initial) < 30:
            self.initial.append(value)
        self.tail.append(value)

    def result(self):
        baseline = sum(self.initial) / len(self.initial) if self.initial else self.peak
        tail = sum(self.tail) / len(self.tail) if self.tail else self.peak
        return {"peak_mb": self.peak, "baseline_mean_mb": baseline, "tail_mean_mb": tail,
                "tail_growth_mb": tail - baseline, "limit_mb": self.maximum,
                "growth_limit_mb": self.growth,
                "valid": self.peak <= self.maximum and tail - baseline <= self.growth}


def phase(plan, now=None):
    elapsed = ((time.monotonic_ns() if now is None else now) - plan["start_monotonic_ns"]) / 1e9
    for name, lo, hi in plan["phases"]:
        if lo <= elapsed < hi:
            return name
    return "normal"


def wait_json(path, seconds):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {path}")
        time.sleep(.1)
    return json.loads(path.read_text())


def gst_imports():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    return GLib, Gst


def capture(args):
    GLib, Gst = gst_imports()
    from cam_driver.config import parse_config
    from cam_driver.gst_frame import GstFrame
    from cam_driver.pipeline import CapturePipeline
    from cam_driver.sources.usb import UsbSource

    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    if (work / "expected.csv").exists():
        raise RuntimeError("use a fresh work directory; expected.csv already exists")
    args.socket_dir.mkdir(parents=True, exist_ok=True)
    socket = args.socket_dir / "unixfd"
    if socket.exists():
        raise RuntimeError(f"socket already exists: {socket}; use an unused socket directory")
    errors, reconnects, buses = [], [], []
    lock = threading.RLock()
    recent = OrderedDict()
    counts = {"capture": 0, "preview": 0, "plugin": 0}
    last_ids = {"preview": -1, "plugin": -1}
    hist = {name: {kind: Histogram() for kind in ("normal", "stress")}
            for name in ("preview", "plugin")}
    normal_counts = {"capture": 0, "preview": 0, "plugin": 0}
    memory = Memory(args.max_rss_mb, args.rss_growth_mb)
    plan, started, base = {}, None, None
    consumer, timer = None, None
    reconnect_index = 0
    cpu_start = None
    first_capture_ns = last_capture_ns = None
    expected_file = (work / "expected.csv").open("w", buffering=1024 * 1024)
    expected = csv.writer(expected_file)
    expected.writerow(("frame_id", "timestamp_ns", "sha256", "phase"))
    metrics_file = (work / "capture_metrics.csv").open("w", buffering=1)
    metrics = csv.writer(metrics_file)
    metrics.writerow(("elapsed_s", "phase", "rss_mb", "capture", "preview", "plugin"))

    def fail(message):
        with lock:
            if len(errors) < 30:
                errors.append(str(message))

    class ReconnectingUsb(UsbSource):
        def __init__(self, cfg):
            super().__init__(cfg)
            self._reconnect = True
            self.inject_disconnect = False
            self.pending_reconnect = None
            self.generated = 0

        def _pipeline_desc(self):
            return super()._pipeline_desc().replace("videotestsrc is-live=true",
                "videotestsrc name=soak_source pattern=ball is-live=true")

        def configure(self):
            super().configure()
            def produced(_pad, _info):
                self.generated += 1
                return Gst.PadProbeReturn.OK
            self._pipeline.get_by_name("soak_source").get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, produced)

        def is_disconnected(self):
            return self.inject_disconnect or super().is_disconnected()

        def reopen(self):
            event = {"reopen_started_ns": time.monotonic_ns(), "last_frame_id": self._frame_id - 1,
                     "last_capture_timestamp_ns": last_capture_ns}
            reconnects.append(event)
            time.sleep(args.reconnect_seconds)
            super().reopen()
            self.inject_disconnect = False
            event["reopen_ready_ns"] = time.monotonic_ns()
            self.pending_reconnect = event

    cfg = parse_config({"camera": {"type": "usb", "frame_rate": args.fps},
        "usb": {"fake": True, "pixel_format": "GRAY8", "width": args.width,
                "height": args.height, "frame_rate": args.fps},
        "recording": {"enabled": True, "output_dir": str(work / "recording"),
                      "encoder": args.encoder, "nvenc_feed": "auto", "segment_seconds": 60},
        "preview": {"enabled": True,
                    "sink": "fakesink name=soak_preview sync=false async=false signal-handoffs=true"},
        "control": {"enabled": False},
        "transport": {"plugin_endpoint": {"enabled": True, "socket_path": str(socket)},
                      "raw_endpoint": {"enabled": False}}})
    source = ReconnectingUsb(cfg.usb)
    pipe = CapturePipeline(cfg, source)
    original_frame = pipe._on_frame
    original_force_quit = pipe._force_quit

    def on_frame(stamp, payload):
        nonlocal first_capture_ns, last_capture_ns
        try:
            if not isinstance(payload, GstFrame):
                raise AssertionError("live capture did not use owned GstFrame delivery")
            digest = hashlib.sha256(bytes(payload)).hexdigest()
            with lock:
                counts["capture"] += 1
                first_capture_ns = first_capture_ns or stamp.timestamp_ns
                last_capture_ns = stamp.timestamp_ns
                kind = phase(plan)
                expected.writerow((stamp.frame_id, stamp.timestamp_ns, digest, kind))
                if kind == "normal":
                    normal_counts["capture"] += 1
                recent[stamp.frame_id] = (stamp.timestamp_ns, digest, kind)
                if len(recent) > 4096:
                    recent.popitem(last=False)
                if source.pending_reconnect is not None:
                    source.pending_reconnect["first_frame_ns"] = time.monotonic_ns()
                    source.pending_reconnect["first_frame_id"] = stamp.frame_id
                    source.pending_reconnect["first_capture_timestamp_ns"] = stamp.timestamp_ns
                    source.pending_reconnect = None
            original_frame(stamp, payload)
        except Exception as exc:
            fail(f"capture callback: {exc}")
            GLib.idle_add(pipe._request_stop_idle)

    pipe._on_frame = on_frame

    def forced_stop():
        fail("main pipeline EOS did not drain; shutdown was forced")
        return original_force_quit()

    pipe._force_quit = forced_stop

    def receipt(_sink, buffer, _pad, name):
        received_ns = time.time_ns()
        try:
            frame_id = int(buffer.offset)
            with lock:
                stamp, digest, frame_phase = recent[frame_id]
            if frame_id <= last_ids[name]:
                raise AssertionError(f"non-increasing frame ID {frame_id} after {last_ids[name]}")
            if name == "plugin" and int(buffer.offset_end) != stamp:
                raise AssertionError("unixfd capture timestamp differs from the source")
            if received_ns < stamp:
                raise AssertionError("capture clock moved backward")
            got = hashlib.sha256(buffer.extract_dup(0, buffer.get_size())).hexdigest()
            if got != digest:
                raise AssertionError(f"pixel mismatch at frame {frame_id}")
            last_ids[name] = frame_id
            kind = phase(plan)
            with lock:
                counts[name] += 1
                if frame_phase == "normal":
                    normal_counts[name] += 1
                hist[name]["normal" if kind == "normal" else "stress"].add((received_ns - stamp) / 1e6)
            if kind == "slow_" + name:
                time.sleep(3 / args.fps)
        except Exception as exc:
            fail(f"{name} callback: {exc}")
            GLib.idle_add(pipe._request_stop_idle)

    def attach():
        nonlocal consumer
        consumer = Gst.parse_launch(f'unixfdsrc socket-path="{socket}" ! '
            'queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! '
            'fakesink name=reader sync=false async=false signal-handoffs=true')
        consumer.get_by_name("reader").connect("handoff", receipt, "plugin")
        bus = consumer.get_bus()
        bus.add_signal_watch()
        buses.append(bus)
        def message(_bus, msg):
            if msg.type == Gst.MessageType.ERROR:
                fail(f"plugin pipeline: {msg.parse_error()}")
                pipe.request_stop()
        bus.connect("message", message)
        consumer.set_state(Gst.State.PLAYING)
        result, state, _ = consumer.get_state(3 * Gst.SECOND)
        if result == Gst.StateChangeReturn.FAILURE or state != Gst.State.PLAYING:
            raise RuntimeError("plugin consumer did not enter PLAYING")

    last_sample = -1

    def tick():
        nonlocal consumer, reconnect_index, last_sample
        try:
            elapsed = (time.monotonic_ns() - started) / 1e9
            if int(elapsed) != last_sample:
                last_sample = int(elapsed)
                value = rss_mb()
                memory.add(elapsed, value)
                metrics.writerow((round(elapsed, 3), phase(plan), round(value, 3),
                                  counts["capture"], counts["preview"], counts["plugin"]))
                expected_file.flush()
            if elapsed >= args.seconds:
                # Join the last admitted callback before closing the recording session.
                # Otherwise a timer could race a manifest write and session.begin_close().
                save(work / "capture.stopping.json", {"monotonic_ns": time.monotonic_ns()})
                source.stop()
                pipe.request_stop()
                return False
            if errors:
                pipe.request_stop()
                return False
            if reconnect_index < 2 and elapsed >= args.seconds * (.25, .75)[reconnect_index]:
                source.inject_disconnect = True
                reconnect_index += 1
            disconnected = phase(plan) == "disconnect_plugin"
            if disconnected and consumer is not None:
                consumer.set_state(Gst.State.NULL)
                consumer = None
            elif not disconnected and consumer is None:
                attach()
            return True
        except Exception as exc:
            fail(f"control timer: {exc}")
            pipe.request_stop()
            return False

    def ready():
        nonlocal timer, started, base, cpu_start
        if not pipe._have_unixfd:
            raise RuntimeError("soak requires unixfd transport")
        if pipe._rec_enc != args.encoder:
            raise RuntimeError(f"expected encoder {args.encoder}, resolved {pipe._rec_enc}")
        attach()
        pipe.pipeline.get_by_name("soak_preview").connect("handoff", receipt, "preview")
        result = pipe.activate()
        if not result.get("ok"):
            raise RuntimeError(f"recording activation failed: {result}")
        base = pipe._session.path_base
        save(work / "ready.json", {"width": args.width, "height": args.height, "fps": args.fps,
             "seconds": args.seconds, "socket": str(socket), "format": "GRAY8", "pattern": "ball",
             "max_rss_mb": args.max_rss_mb, "rss_growth_mb": args.rss_growth_mb})
        if not args.skip_ros:
            wait_json(work / "ros.ready.json", 90)
        started = time.monotonic_ns()
        cpu_start = time.process_time()
        plan.update({"start_monotonic_ns": started, "seconds": args.seconds,
            "phases": [(name, lo * args.seconds, hi * args.seconds) for name, lo, hi in (
                ("slow_preview", .15, .20), ("slow_plugin", .35, .40), ("slow_ros", .50, .55),
                ("disconnect_plugin", .60, .62), ("disconnect_ros", .65, .67))],
            "source_reconnect_at_s": [args.seconds * .25, args.seconds * .75],
            "reconnect_hold_s": args.reconnect_seconds})
        save(work / "plan.json", plan)
        timer = GLib.timeout_add(100, tick)
        return True

    try:
        source.open()
        source.configure()
        pipe.build()
        pipe.run(on_playing=ready)
    except Exception as exc:
        fail(f"capture run: {exc}")
    finally:
        if timer is not None:
            context = GLib.MainContext.default()
            if context.find_source_by_id(timer) is not None:
                GLib.source_remove(timer)
        if consumer is not None:
            consumer.set_state(Gst.State.NULL)
        for bus in buses:
            bus.remove_signal_watch()
        pipe.shutdown()
        expected_file.close()
        metrics_file.close()
        save(work / "capture.stopped.json", {"monotonic_ns": time.monotonic_ns()})
    health = pipe.drops.summary()
    if pipe.had_error:
        fail("CapturePipeline reported an error")
    if any(health[key] for key in ("source_gaps", "frames_missing", "enqueue_failures")):
        fail(f"captured/recorded frame loss: {health}")
    if counts["capture"] != health["frames"] or not counts["capture"]:
        fail("capture manifest count differs from pipeline frame count")
    if len(reconnects) != 2 or any("first_frame_id" not in event for event in reconnects):
        fail("both source reconnects did not complete")
    reconnect_silence = sum(max(0, (event["first_capture_timestamp_ns"] - event["last_capture_timestamp_ns"])
        / 1e9 - 1 / args.fps) for event in reconnects
        if event.get("first_capture_timestamp_ns") and event.get("last_capture_timestamp_ns"))
    active_span = ((last_capture_ns - first_capture_ns) / 1e9 - reconnect_silence
                   if first_capture_ns and last_capture_ns else 0)
    achieved_fps = (counts["capture"] - 1) / active_span if active_span > 0 else 0
    if achieved_fps < .95 * args.fps:
        fail(f"capture achieved {achieved_fps:.3f} FPS excluding reconnect silence; expected >=95% of {args.fps}")
    # videotestsrc's source-pad probe precedes admission to the capture callback. A NULL
    # transition may cancel one in-flight buffer per stop/reconnect, so report that bound.
    unadmitted = source.generated - counts["capture"]
    if not 0 <= unadmitted <= len(reconnects) + 1:
        fail(f"generated/admitted difference {unadmitted} exceeds stop-boundary allowance")
    if not memory.result()["valid"]:
        fail("capture RSS exceeded the declared peak/growth budget")
    # Allow a small boundary backlog when phases change, but fail a stalled normal consumer.
    for name in ("preview", "plugin"):
        if normal_counts[name] < .95 * normal_counts["capture"]:
            fail(f"{name} received under 95% of normally captured frames")
    result = {"valid": not errors, "errors": errors, "gstreamer": Gst.version_string(),
        "recording_base": base, "encoder": pipe._rec_enc, "feed": pipe._rec_feed,
        "native_unixfd_pool": pipe._unixfd_pool, "health": health, "counts": counts,
        "normal_phase_counts": normal_counts, "source_generated": source.generated,
        "generated_but_not_admitted": unadmitted, "stop_boundary_allowance": len(reconnects) + 1,
        "achieved_fps_excluding_reconnect_silence": achieved_fps,
        "reconnect_silence_seconds_beyond_nominal_frame_interval": reconnect_silence,
        "source_reconnects": reconnects, "rss": memory.result(),
        "latency": {name: {kind: value.result() for kind, value in kinds.items()} for name, kinds in hist.items()},
        "cpu_seconds": time.process_time() - cpu_start if cpu_start is not None else None,
        "capture_and_shutdown_seconds": (time.monotonic_ns() - started) / 1e9 if started else None,
        "session_result": pipe._last_result, "skip_ros": args.skip_ros,
        "instrumentation": "full SHA256 for every capture, preview and plugin receipt; bounded metadata/histograms",
        "latency_scope": "service host-arrival capture timestamp to local callback receipt; headless preview"}
    save(work / "capture_result.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["valid"] else 1


def ros_reader(args):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image
    work = args.work
    config = wait_json(work / "ready.json", 120)
    rclpy.init()
    node = rclpy.create_node("camera_combined_soak_reader")
    errors, count, last_stamp = [], 0, -1
    histogram = {kind: Histogram() for kind in ("normal", "stress")}
    memory = Memory(config["max_rss_mb"], config["rss_growth_mb"])
    bridge_memory = Memory(config["max_rss_mb"], config["rss_growth_mb"])
    plan = None
    output = (work / "ros.csv").open("w", buffering=1024 * 1024)
    writer = csv.writer(output)
    writer.writerow(("timestamp_ns", "receipt_ns", "sha256", "phase"))
    metrics_file = (work / "ros_metrics.csv").open("w", buffering=1)
    metrics = csv.writer(metrics_file)
    metrics.writerow(("elapsed_s", "phase", "reader_rss_mb", "bridge_rss_mb", "received"))
    def callback(message):
        nonlocal count, last_stamp
        receipt_ns = time.time_ns()
        stamp = message.header.stamp.sec * 10**9 + message.header.stamp.nanosec
        current = phase(plan) if plan else "normal"
        problem = None
        if (message.width, message.height, message.step, message.encoding, message.is_bigendian,
                message.header.frame_id, len(message.data)) != (
                config["width"], config["height"], config["width"], "mono8", 0,
                "optimization_bench", config["width"] * config["height"]):
            problem = "ROS geometry/encoding/stride/frame_id differs"
        elif stamp <= last_stamp:
            problem = "ROS timestamps are not strictly increasing"
        elif receipt_ns < stamp:
            problem = "capture clock moved backward"
        if problem and len(errors) < 30:
            errors.append(problem)
        last_stamp = stamp
        writer.writerow((stamp, receipt_ns, hashlib.sha256(memoryview(message.data)).hexdigest(), current))
        count += 1
        histogram["normal" if current == "normal" else "stress"].add((receipt_ns - stamp) / 1e6)
        if current == "slow_ros":
            time.sleep(3 / config["fps"])
    def subscribe():
        return node.create_subscription(Image, "/optimization_bench/image", callback, qos_profile_sensor_data)
    subscription = subscribe()
    stop_at, last_sample = None, -1
    bridge_exited_during_shutdown = False
    deadline = time.monotonic() + config["seconds"] + 180
    try:
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.05)
            if not (work / "ros.ready.json").exists() and node.count_publishers("/optimization_bench/image"):
                save(work / "ros.ready.json", {"pid": os.getpid(), "publisher_discovered": True})
            if plan is None and (work / "plan.json").exists():
                plan = json.loads((work / "plan.json").read_text())
            if plan is not None:
                elapsed = (time.monotonic_ns() - plan["start_monotonic_ns"]) / 1e9
                current = phase(plan)
                if current == "disconnect_ros" and subscription is not None:
                    node.destroy_subscription(subscription)
                    subscription = None
                elif current != "disconnect_ros" and subscription is None:
                    subscription = subscribe()
                if int(elapsed) != last_sample:
                    last_sample = int(elapsed)
                    own_rss = rss_mb()
                    bridge_rss = None
                    if args.bridge_pid:
                        try:
                            bridge_rss = rss_mb(args.bridge_pid)
                        except FileNotFoundError:
                            if not ((work / "capture.stopping.json").exists() or
                                    (work / "capture.stopped.json").exists()):
                                raise RuntimeError("ROS bridge exited before capture shutdown")
                            bridge_exited_during_shutdown = True
                    memory.add(elapsed, own_rss)
                    if bridge_rss is not None:
                        bridge_memory.add(elapsed, bridge_rss)
                    metrics.writerow((round(elapsed, 3), current, round(own_rss, 3),
                                      round(bridge_rss, 3) if bridge_rss is not None else "", count))
                    output.flush()
            if (work / "capture.stopped.json").exists():
                stop_at = stop_at or time.monotonic() + 2
                if time.monotonic() >= stop_at:
                    break
        else:
            errors.append("ROS reader exceeded its capture/drain deadline")
    except Exception as exc:
        errors.append(f"ROS reader run: {exc}")
    finally:
        node.destroy_node()
        rclpy.shutdown()
        output.close()
        metrics_file.close()
    if not count:
        errors.append("ROS received no frames")
    if not memory.result()["valid"] or (args.bridge_pid and not bridge_memory.result()["valid"]):
        errors.append("ROS reader/bridge RSS exceeded the declared peak/growth budget")
    result = {"valid": not errors, "errors": errors, "received": count, "rss": memory.result(),
              "bridge_rss": bridge_memory.result() if args.bridge_pid else None,
              "bridge_exited_during_shutdown": bridge_exited_during_shutdown,
              "latency": {kind: value.result() for kind, value in histogram.items()},
              "latency_scope": "service host-arrival capture timestamp to local ROS callback receipt"}
    save(work / "ros_result.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["valid"] else 1


def verify(args):
    GLib, _Gst = gst_imports()
    from cam_driver.config import parse_config
    from cam_driver.sources.replay import ReplaySource
    work = args.work
    capture_result = wait_json(work / "capture_result.json", 10)
    errors = list(capture_result["errors"])
    base = Path(capture_result["recording_base"])
    total = capture_result["counts"]["capture"]
    session_result = capture_result["session_result"] or {}
    session = session_result.get("session", {})
    if not session_result.get("ok") or session.get("truncated") or session.get("error"):
        errors.append(f"recording session did not finalize cleanly: {session}")
    if session.get("frames") != total or not session.get("segments"):
        errors.append("final recording session frame/segment accounting differs")
    sidecar_count = 0
    with (work / "expected.csv").open() as ef, base.with_suffix(".csv").open() as sf:
        expected, sidecar = csv.DictReader(ef), csv.DictReader(sf)
        for e in expected:
            s = next(sidecar, None)
            if s is None or (int(s["frame_id"]), int(s["timestamp_ns"])) != (int(e["frame_id"]), int(e["timestamp_ns"])):
                errors.append(f"recording CSV differs at captured frame {e['frame_id']}")
                break
            sidecar_count += 1
        if next(sidecar, None) is not None:
            errors.append("recording CSV has extra rows")
    decode_start, decoded = time.monotonic(), 0
    replay_cfg = parse_config({"camera": {"type": "replay"}, "replay": {"path": str(base), "speed": 0}})
    replay = ReplaySource(replay_cfg.replay, replay_cfg.playback)
    with (work / "expected.csv").open() as stream:
        expected = csv.DictReader(stream)
        def frame(stamp, payload):
            nonlocal decoded
            e = next(expected, None)
            actual = (stamp.frame_id, stamp.timestamp_ns, hashlib.sha256(bytes(payload)).hexdigest())
            if e is None or actual != (int(e["frame_id"]), int(e["timestamp_ns"]), e["sha256"]):
                if len(errors) < 30:
                    errors.append(f"decoded pixels/ID/timestamp differ at output frame {decoded}")
            decoded += 1
        try:
            replay.open()
            replay.configure()
            replay.start_buffers(frame)
            context = GLib.MainContext.default()
            while not replay.finished:
                while context.iteration(False):
                    pass
                if time.monotonic() - decode_start > args.validation_timeout:
                    raise TimeoutError("full recording validation timed out")
                time.sleep(.002)
            if replay.finished_error:
                errors.append("recording replay finished with an error")
            if next(expected, None) is not None:
                errors.append("recording decode ended before all captured frames")
        finally:
            replay.stop()
    if sidecar_count != total or decoded != total:
        errors.append(f"capture/CSV/decode counts differ: {total}/{sidecar_count}/{decoded}")
    validation_seconds = time.monotonic() - decode_start
    ros = None
    if not capture_result["skip_ros"]:
        ros = wait_json(work / "ros_result.json", 15)
        errors.extend(ros["errors"])
        matched, normal_received, normal_captured = 0, 0, 0
        with (work / "expected.csv").open() as ef, (work / "ros.csv").open() as rf:
            expected, received = csv.DictReader(ef), csv.DictReader(rf)
            e = next(expected, None)
            for r in received:
                while e is not None and int(e["timestamp_ns"]) < int(r["timestamp_ns"]):
                    if e["phase"] == "normal":
                        normal_captured += 1
                    e = next(expected, None)
                if e is None or (e["timestamp_ns"], e["sha256"]) != (r["timestamp_ns"], r["sha256"]):
                    errors.append(f"ROS stamp/pixels are not an exact captured frame at receipt {matched}")
                    break
                if e["phase"] == "normal":
                    normal_captured += 1
                    normal_received += 1
                matched += 1
                e = next(expected, None)
            while e is not None:
                if e["phase"] == "normal":
                    normal_captured += 1
                e = next(expected, None)
        if matched != ros["received"]:
            errors.append("ROS verified count differs from received count")
        if normal_received < .95 * normal_captured:
            errors.append("ROS received under 95% of normally captured frames")
        ros["verified_exact_frames"] = matched
        ros["normal_phase_counts"] = {"captured": normal_captured, "received": normal_received}
    result = {"valid": not errors, "errors": errors, "capture": capture_result, "ros": ros,
              "recording": {"captured": total, "csv_rows": sidecar_count, "decoded": decoded,
                  "all_pixels_ids_timestamps_exact": decoded == total and sidecar_count == total and not errors,
                  "full_decode_validation_seconds": validation_seconds,
                  "decoded_uncompressed_gb": total * json.loads((work / "ready.json").read_text())["width"]
                      * json.loads((work / "ready.json").read_text())["height"] / 1e9},
              "notes": ["Synthetic moving ball; this is combined-load correctness, not a camera/sensor throughput claim.",
                  "Best-effort preview/plugin/ROS gaps during stress are distinct from captured recording loss.",
                  "Injected source reopen silence is downtime, not missing captured/recorded frames."]}
    save(work / "summary.json", result)
    print(json.dumps(result), flush=True)
    return 0 if result["valid"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture")
    cap.add_argument("--work", type=Path, required=True)
    cap.add_argument("--socket-dir", type=Path, required=True)
    cap.add_argument("--seconds", type=float, default=600)
    cap.add_argument("--width", type=int, default=2448)
    cap.add_argument("--height", type=int, default=2048)
    cap.add_argument("--fps", type=int, default=24)
    cap.add_argument("--encoder", default="hw-hevc-lossless", choices=("hw-hevc-lossless", "ffv1"))
    cap.add_argument("--reconnect-seconds", type=float, default=1)
    cap.add_argument("--max-rss-mb", type=float, default=2048)
    cap.add_argument("--rss-growth-mb", type=float, default=256)
    cap.add_argument("--skip-ros", action="store_true", help="development smoke only; omits ROS assertions")
    ros = commands.add_parser("ros-reader")
    ros.add_argument("--work", type=Path, required=True)
    ros.add_argument("--bridge-pid", type=int, default=os.environ.get("SOAK_BRIDGE_PID"))
    val = commands.add_parser("verify")
    val.add_argument("--work", type=Path, required=True)
    val.add_argument("--validation-timeout", type=float, default=1800)
    args = parser.parse_args()
    if args.command == "capture" and (args.seconds < 20 or min(args.width, args.height, args.fps) <= 0):
        parser.error("capture requires at least 20 seconds and positive geometry/FPS")
    return {"capture": capture, "ros-reader": ros_reader, "verify": verify}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
