#!/usr/bin/env python3
"""Generate a deterministic multi-fragment FFV1 recording, or measure a late replay window.

Generate once, then run separate processes against the same fixture and different source
snapshots. File hashes are checked before timing, so results describe a warm filesystem
cache. Every delivered pixel, frame ID and timestamp is verified, including restart.

  python3 replay_window_benchmark.py generate /reports/window-fixture
  python3 replay_window_benchmark.py run /reports/window-fixture --source-root /before
  python3 replay_window_benchmark.py run /reports/window-fixture --source-root /after
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

T0 = 1_800_000_000_000_000_000


def pixels(index, size):
    return index.to_bytes(8, "little") + bytes([index % 251]) * (size - 8)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate(args, Gst):
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=False)
    base = directory / "window"
    interval = 1_000_000_000 // args.fps
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time block=true max-buffers=4 max-bytes=0 caps="video/x-raw,format=GRAY8,'
        f'width={args.width},height={args.height},framerate={args.fps}/1" ! '
        'avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1 ! '
        f'splitmuxsink muxer=matroskamux max-size-time={args.segment_seconds * Gst.SECOND} '
        f'location="{base}-%05d.mkv"')
    source = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for index in range(args.frames):
            buffer = Gst.Buffer.new_wrapped(pixels(index, args.width * args.height))
            buffer.pts, buffer.duration = index * interval, interval
            if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
                raise RuntimeError("fixture encoder stopped")
        source.emit("end-of-stream")
        message = pipeline.get_bus().timed_pop_filtered(120 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None or message.type != Gst.MessageType.EOS:
            raise RuntimeError(message.parse_error() if message else "fixture encode timeout")
    finally:
        pipeline.set_state(Gst.State.NULL)
    parts = sorted(directory.glob("window-*.mkv"))
    if len(parts) < 3:
        raise RuntimeError("benchmark needs at least three fragments")
    header = {"pixel_format": "GRAY8", "bits_per_pixel": 8, "width": args.width, "height": args.height,
              "base_timestamp_ns": T0, "first_timestamp_ns": T0, "first_pts_ns": 0,
              "recording_encoder": "ffv1", "cfa_tile_mode": "off",
              "session": {"frames_recorded": args.frames, "segments": len(parts), "truncated": False, "error": None}}
    base.with_suffix(".json").write_text(json.dumps(header, indent=2) + "\n")
    with base.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frame_id", "pts_ns", "timestamp_ns", "source", "chunk_ns", "camera_ns", "system_ns"])
        for i in range(args.frames):
            ts = T0 + i * interval
            writer.writerow([i, i * interval, ts, "ptp_chunk", ts, ts + 1, ts + 2])
    info = {"frames": args.frames, "width": args.width, "height": args.height, "fps": args.fps,
            "interval_ns": interval, "parts": len(parts), "first_window_frame": args.first_window_frame,
            "files": {path.name: file_hash(path) for path in [*parts, base.with_suffix('.json'), base.with_suffix('.csv')]}}
    (directory / "benchmark-fixture.json").write_text(json.dumps(info, indent=2) + "\n")
    print(json.dumps(info), flush=True)


def run(args, Gst, GLib):
    root = Path(args.source_root).resolve()
    sys.path.insert(0, str(root / "core-driver"))
    from cam_driver.config import parse_config
    from cam_driver.sources import replay
    directory = Path(args.directory)
    info = json.loads((directory / "benchmark-fixture.json").read_text())
    for name, expected in info["files"].items():
        assert file_hash(directory / name) == expected, "fixture changed: " + name
    first, interval = info["first_window_frame"], info["interval_ns"]
    cfg = parse_config({"camera": {"type": "replay"}, "replay": {"path": str(directory), "speed": 0},
                        "playback": {"epoch_unix_ns": T0 + first * interval, "from_s": 0},
                        "control": {"enabled": False}})
    decoded, scanned = [0], [0]
    scan = getattr(replay, "_index_fragment", None)
    if scan is not None:
        def index_fragment(*values, **kwargs):
            result = scan(*values, **kwargs)
            scanned[0] += result[0]
            return result
        replay._index_fragment = index_fragment
    result = {"source_root": str(root), "gstreamer": Gst.version_string(), "fixture": info,
              "source_sha256": file_hash(root / "core-driver/cam_driver/sources/replay.py"),
              "cache": "fixture hashes read before timing; warm filesystem cache", "phases": []}
    start = time.monotonic()
    source = replay.ReplaySource(cfg.replay, cfg.playback)
    if args.full_decode:
        source._window_start = lambda _idx: (0, 0)
    frames, errors, first_at = [], [], [None]
    try:
        source.open()
        source.configure()
        stamp_frame = source._new_stamp
        def new_stamp(buffer):
            decoded[0] += 1
            return stamp_frame(buffer)
        source._new_stamp = new_stamp
        def receive(stamp, data):
            if first_at[0] is None:
                first_at[0] = time.monotonic()
            try:
                assert first <= stamp.frame_id < info["frames"]
                assert stamp.timestamp_ns == T0 + stamp.frame_id * interval + source._cycle * source._span_ns
                assert bytes(data) == pixels(stamp.frame_id, info["width"] * info["height"])
                frames.append((stamp.frame_id, stamp.timestamp_ns))
            except BaseException as error:
                errors.append(repr(error))
        context = GLib.MainContext.default()
        for phase in ("first_start", "restart"):
            prior_frames, prior_decoded, prior_scanned = len(frames), decoded[0], scanned[0]
            if phase == "first_start":
                # Include the compressed index built during open() in the first phase.
                prior_scanned = 0
                source.start_buffers(receive)
            else:
                first_at[0] = None
                start = time.monotonic()
                assert source.playback.request("restart")["ok"]
            deadline = time.monotonic() + 180
            while not source.finished:
                while context.iteration(False):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("replay did not finish")
                time.sleep(.001)
            assert not source.finished_error and not errors, errors
            assert [fid for fid, _ in frames[prior_frames:]] == list(range(first, info["frames"]))
            result["phases"].append({"phase": phase, "open_or_restart_to_first_frame_s": first_at[0] - start,
                                     "complete_s": time.monotonic() - start,
                                     "decoded_frames": decoded[0] - prior_decoded,
                                     "compressed_packets_scanned": scanned[0] - prior_scanned,
                                     "delivered_frames": len(frames) - prior_frames,
                                     "pixels_ids_timestamps_exact": True})
        result["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    finally:
        source.stop()
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("generate", "run"))
    parser.add_argument("directory")
    parser.add_argument("--source-root", default=str(Path.cwd()))
    parser.add_argument("--output")
    parser.add_argument("--full-decode", action="store_true", help="disable indexing to isolate its effect on the same source snapshot")
    parser.add_argument("--frames", type=int, default=3600)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--segment-seconds", type=int, default=10)
    parser.add_argument("--first-window-frame", type=int, default=3450)
    args = parser.parse_args()
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
    Gst.init(None)
    (generate(args, Gst) if args.mode == "generate" else run(args, Gst, GLib))


if __name__ == "__main__":
    main()
