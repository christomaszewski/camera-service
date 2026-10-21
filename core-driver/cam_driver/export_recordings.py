"""Camera-owned offline recording exports. Host preflight needs only the Python stdlib.

The media path imports GStreamer lazily and runs in the native cam-dev image. It uses
the same session discovery, stamp parser and decoder policy as replay, with bounded
queues and no pacing, plugins, transport, camera discovery or recording fallback.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading

from . import playback
from .formats import parse_pixel_format

CONTRACT_VERSION = 1
FORMAT = "camera-recording-proxy/v1"


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_relative(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or any(p in (".", "..") for p in relative.split("/"))):
        raise ValueError(f"unsafe recording path: {relative!r}")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"recording path escapes its root: {relative}")
    return path


def profile_definition(request, profile_root):
    profile = request.get("profile", {})
    ident = profile.get("id")
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", ident):
        raise ValueError("invalid export profile ID")
    path = Path(profile_root) / (ident + ".yaml")
    if not path.is_file():
        raise ValueError(f"converter does not provide profile {ident!r}; update the cam-dev image")
    # JSON is a YAML subset. Keep bundled profiles readable on stock host Python.
    definition = json.loads(path.read_text())
    digest = canonical_hash(definition)
    if digest != profile.get("sha256") or canonical_hash(profile.get("definition")) != digest:
        raise ValueError("profile definition/hash differs from the converter's bundled profile; "
                         "build or pull the matching cam-dev image")
    if (definition.get("version") != 1 or definition.get("id") != ident
            or definition.get("format") != FORMAT or definition.get("lossy") is not True):
        raise ValueError("unsupported camera export profile contract")
    return definition


def _rows(run):
    # Reuse replay's parsing/validation of stamp fields; preserve CSV fields verbatim later.
    stamps = playback.load_stamps(run.csv_path)
    with open(run.csv_path, newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    if not stamps or "pts_ns" not in (fields or ()):
        raise ValueError("recording needs nonempty frame stamps and pts_ns")
    times = [stamp.timestamp_ns for stamp in stamps]
    pts = [int(row["pts_ns"]) for row in rows]
    if min(times) < 0 or any(b < a for a, b in zip(times, times[1:])):
        raise ValueError("capture timestamps must be nonnegative and ordered")
    if min(pts) < 0 or any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError("media PTS must be nonnegative and strictly increasing")
    if any(row.get(None) for row in rows):
        raise ValueError("CSV contains extra unlabelled fields")
    summary = run.header.get("session") or {}
    if summary.get("frames_recorded", len(rows)) != len(rows):
        raise ValueError("CSV frame count differs from recording session attestation")
    return fields, rows, stamps


def preflight(request, profile_root):
    if request.get("version") != CONTRACT_VERSION:
        raise ValueError("unsupported camera export request version")
    definition = profile_definition(request, profile_root)
    source = Path(request["source_root"]).resolve(strict=True)
    selections = request.get("recordings")
    if not isinstance(selections, list) or not selections:
        raise ValueError("export request must select recordings")
    result, errors, names = [], [], set()
    for item in selections:
        name = item.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                or name in (".", "..") or name in names):
            raise ValueError(f"invalid or duplicate recorded instance name: {name!r}")
        names.add(name)
        relative = item.get("relative")
        if relative != f"recordings/{name}":
            raise ValueError(f"camera recording must be recordings/{name}, got {relative!r}")
        directory = safe_relative(source, relative)
        requested = item.get("sessions") or None
        if requested:
            requested = [s["id"] if isinstance(s, dict) else s for s in requested]
        try:
            sessions = playback.discover_sessions(str(directory), sessions=requested)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        selected = []
        for run in sessions:
            ident = Path(run.base).name
            try:
                playback.check_glob_hazards(run)
                if any(c in str(run.base) for c in '*?[]"\\\n\r'):
                    raise ValueError("recording path contains characters unsupported by GStreamer")
                hdr = run.header
                pf = hdr["pixel_format"]
                fmt, bits, bayer, packed, _ = parse_pixel_format(pf)
                if pf not in definition["supported_pixel_formats"] or bits > 8 or packed:
                    raise ValueError(f"h264 profile does not support {pf}; keep this instance on originals")
                if int(hdr.get("bits_per_pixel", bits)) > 8:
                    raise ValueError("h264 profile cannot truncate high-bit-depth recording data")
                w, h = int(hdr["width"]), int(hdr["height"])
                if w <= 0 or h <= 0 or w % 2 or h % 2:
                    raise ValueError("h264 profile requires positive even width and height")
                tile = hdr.get("cfa_tile_mode", "off")
                if tile not in ("off", False, None, "plain", True, "green_diff", "rct"):
                    raise ValueError(f"unknown CFA storage tiling: {tile}")
                if tile not in ("off", False, None) and (fmt != "GRAY8" or not (bayer or hdr.get("bayer_pattern"))):
                    raise ValueError("CFA tiling requires an 8-bit Bayer recording")
                _, rows, stamps = _rows(run)
                files = [run.base + ".json", run.csv_path, *run.mkv_paths]
                paths = []
                for value in files:
                    rel = Path(value).relative_to(source).as_posix()
                    checked = safe_relative(source, rel)
                    if not checked.is_file():
                        raise ValueError(f"missing input file {rel}")
                    paths.append(rel)
                selected.append({"id": ident, "files": paths, "frame_count": len(rows),
                                 "start_ns": stamps[0].timestamp_ns, "end_ns": stamps[-1].timestamp_ns,
                                 "width": w, "height": h, "pixel_format": pf,
                                 "bayer_pattern": hdr.get("bayer_pattern"), "storage_format": "I420",
                                 "lossy": True, "format": FORMAT})
            except (ValueError, OSError, KeyError, TypeError) as exc:
                errors.append(f"{name}/{ident}: {exc}")
        result.append({"name": name, "relative": relative, "sessions": selected})
    if errors:
        raise ValueError("unsupported or incomplete recordings:\n  " + "\n  ".join(errors))
    return {"version": CONTRACT_VERSION, "profile_sha256": canonical_hash(definition),
            "recordings": result, "validation": {"ok": True},
            "producer": {"converter": "camera-service", "contract": CONTRACT_VERSION,
                         "implementation_sha256": file_hash(__file__)}}


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def _decode(run):
    """Blocking raw samples, including stream-copy inputs: no best-effort decode drops."""
    from .sources.replay import _check_glob_hazards, _probe_mkv, _RAW_DECODE
    Gst = _gst()
    _check_glob_hazards(run)
    codec = _probe_mkv(run.mkv_paths[0])
    if codec not in _RAW_DECODE:
        raise ValueError(f"unsupported/unreadable recording codec {codec}: {run.base}")
    fmt = parse_pixel_format(run.header["pixel_format"])[0]
    pipeline = Gst.parse_launch(
        f'splitmuxsrc location="{run.mkv_glob}" ! {_RAW_DECODE[codec]} ! videoconvert n-threads=2 ! '
        f'video/x-raw,format={fmt},width={int(run.header["width"])},height={int(run.header["height"])} ! '
        'appsink name=sink sync=false max-buffers=4 drop=false')
    sink, bus = pipeline.get_by_name("sink"), pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        while True:
            sample = sink.emit("try-pull-sample", 30 * Gst.SECOND)
            if sample is not None:
                yield sample
                continue
            msg = bus.timed_pop_filtered(Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None and msg.type == Gst.MessageType.EOS:
                return
            if msg is not None:
                error, debug = msg.parse_error()
                raise ValueError(f"recording decode failed: {error}: {debug}")
            raise ValueError(f"recording decode timed out: {run.base}")
    finally:
        pipeline.set_state(Gst.State.NULL)


def _untile_buffer(sample, header):
    """Undo storage CFA tiling, preserving GStreamer row padding for even widths."""
    from .bayer_tile import normalize_mode, untile_cfa
    mode = normalize_mode(header.get("cfa_tile_mode"))
    buf = sample.get_buffer()
    if mode == "off":
        return buf.copy_deep()
    import gi
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo
    Gst = _gst()
    info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
    w, h = int(header["width"]), int(header["height"])
    raw = buf.extract_dup(0, buf.get_size())
    stride = info.stride[0]
    packed = b"".join(raw[info.offset[0] + y * stride:info.offset[0] + y * stride + w] for y in range(h))
    untiled = untile_cfa(packed, w, h, mode,
                        header.get("bayer_pattern") or parse_pixel_format(header["pixel_format"])[2] or "rggb")
    data = bytearray(raw)
    for y in range(h):
        offset = info.offset[0] + y * stride
        data[offset:offset + w] = untiled[y * w:(y + 1) * w]
    return Gst.Buffer.new_wrapped(bytes(data))


def _encode(run, target, definition, rows, stamps):
    if any(c in str(target) for c in '*?[]"\\\n\r'):
        raise ValueError("export destination contains characters unsupported by GStreamer")
    Gst = _gst()
    settings = definition["encoding"]
    interval = playback.median_interval_ns([stamp.timestamp_ns for stamp in stamps])
    fps = 1e9 / interval
    keyframes = max(1, int(round(fps * settings["keyframe_interval_s"])))
    pipeline = Gst.parse_launch(
        'appsrc name=src format=time is-live=false block=true max-buffers=4 max-bytes=0 ! '
        'videoconvert n-threads=2 ! video/x-raw,format=I420 ! '
        f'x264enc pass=qual quantizer={settings["crf"]} speed-preset={settings["preset"]} '
        f'tune=zerolatency bframes={settings["bframes"]} ref={settings["reference_frames"]} '
        f'cabac={str(settings["cabac"]).lower()} dct8x8=false '
        f'key-int-max={keyframes} ! video/x-h264,profile={settings["profile"]} ! h264parse ! '
        f'splitmuxsink muxer=matroskamux max-size-time={int(settings["segment_seconds"] * 1e9)} '
        f'location="{target}-%05d.mkv"')
    src, bus = pipeline.get_by_name("src"), pipeline.get_bus()
    completed, failed = threading.Event(), []

    def monitor():
        while not completed.is_set():
            msg = bus.timed_pop_filtered(Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    error, debug = msg.parse_error()
                    failed.append(f"h264 encoding failed: {error}: {debug}")
                    pipeline.set_state(Gst.State.NULL)  # release a blocked appsrc producer
                completed.set()

    worker = threading.Thread(target=monitor, daemon=True)
    worker.start()
    count = 0
    decode_pts_offset = None
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for sample in _decode(run):
            if count >= len(rows):
                raise ValueError(f"{run.base}: decoded more frames than the CSV contains")
            if not count:
                src.set_property("caps", sample.get_caps())
                decode_pts_offset = int(sample.get_buffer().pts) - int(rows[0]["pts_ns"])
            if abs(int(sample.get_buffer().pts) - decode_pts_offset - int(rows[count]["pts_ns"])) > 1_000_000:
                raise ValueError(f"{run.base}: source frame {count} does not align with its CSV PTS")
            buf = _untile_buffer(sample, run.header)
            # Capture stamps are unchanged. The original sidecar PTS is the authoritative
            # muxing timeline; decode PTS may include container offsets/rounding.
            buf.pts = int(rows[count]["pts_ns"])
            buf.dts = Gst.CLOCK_TIME_NONE
            buf.duration = (int(rows[count + 1]["pts_ns"]) - buf.pts) if count + 1 < len(rows) else interval
            buf.offset = stamps[count].frame_id
            if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                raise ValueError(failed[0] if failed else "h264 encoder stopped before all frames were accepted")
            count += 1
        if count != len(rows):
            raise ValueError(f"{run.base}: CSV has {len(rows)} rows but {count} frames decoded")
        src.emit("end-of-stream")
        if not completed.wait(60):
            raise ValueError("h264 encoder did not finish within 60 seconds")
        if failed:
            raise ValueError(failed[0])
    finally:
        completed.set()
        pipeline.set_state(Gst.State.NULL)
        worker.join(2)


def convert(request, profile_root):
    result = preflight(request, profile_root)
    definition = profile_definition(request, profile_root)
    root = Path(request["source_root"]).resolve(strict=True)
    destination = Path(request["destination"]).resolve(strict=True)
    if destination == root or root.is_relative_to(destination):
        raise ValueError("export destination must not be the source root or its parent")
    if any(destination.iterdir()):
        raise ValueError("converter destination must be an empty staging directory")
    # Require all encoders before writing even the first session.
    Gst = _gst()
    for plugin in ("x264enc", "h264parse", "avdec_h264", "splitmuxsrc", "splitmuxsink", "matroskamux"):
        if Gst.ElementFactory.find(plugin) is None:
            raise ValueError(f"cam-dev image is missing required GStreamer element {plugin}")
    result["producer"]["gstreamer"] = Gst.version_string()
    for recording in result["recordings"]:
        outdir = safe_relative(destination, recording["relative"])
        outdir.mkdir(parents=True)
        for session in recording["sessions"]:
            prefix = session["id"]
            run = playback.discover_sessions(str(root / recording["relative"]), prefix)[0]
            fields, rows, stamps = _rows(run)
            source_hashes = {p: file_hash(safe_relative(root, p)) for p in session["files"]}
            target = outdir / prefix
            _encode(run, target, definition, rows, stamps)
            parts = sorted(outdir.glob(prefix + "-*.mkv"))
            header = dict(run.header)
            # Preserve capture metadata, regenerate storage-dependent fields and summaries.
            header.update({"recording_encoder": "x264", "recording_lossy": True,
                           "bayer_pattern": header.get("bayer_pattern") or parse_pixel_format(header["pixel_format"])[2],
                           "recording_settings": definition["encoding"], "recording_settings_file": None,
                           "cfa_tile_mode": "off", "pts_offset_ns": 0,
                           "first_pts_ns": int(rows[0]["pts_ns"]),
                           "first_timestamp_ns": stamps[0].timestamp_ns,
                           "first_frame_id": stamps[0].frame_id, "session_prefix": prefix,
                           "session": {"frames_recorded": len(rows), "segments": len(parts),
                                       "truncated": False, "error": None},
                           "proxy": {"version": 1, "format": FORMAT, "profile": definition["id"],
                                     "profile_revision": definition["revision"],
                                     "profile_sha256": result["profile_sha256"], "lossy": True,
                                     "recorded_as": recording["name"], "session": prefix,
                                     "source_files": source_hashes,
                                     "source_session": run.header.get("session"),
                                     "transformations": ["decode", "undo-cfa-tiling", "convert-to-i420", "lossy-h264"]}})
            Path(str(target) + ".json").write_text(json.dumps(header, indent=2, sort_keys=True) + "\n")
            with open(str(target) + ".csv", "w", newline="") as stream:
                writer = csv.DictWriter(stream, fields)
                writer.writeheader()
                writer.writerows(rows)
            output = playback.discover_sessions(str(outdir), prefix)[0]
            _, output_rows, output_stamps = _rows(output)
            decoded = 0
            pts_offset = None
            for sample in _decode(output):
                if decoded >= len(rows):
                    raise ValueError(f"{prefix}: proxy decoded more frames than the input")
                # splitmuxsrc rebases its timeline (currently starting at 1000 seconds).
                # Validate every relative PTS within Matroska's millisecond precision;
                # absolute capture nanoseconds stay unchanged in CSV.
                actual_pts = int(sample.get_buffer().pts)
                if pts_offset is None:
                    pts_offset = actual_pts - int(rows[0]["pts_ns"])
                if abs(actual_pts - pts_offset - int(rows[decoded]["pts_ns"])) > 1_000_000:
                    raise ValueError(f"{prefix}: proxy frame {decoded} PTS {sample.get_buffer().pts} "
                                     f"does not match its CSV PTS {rows[decoded]['pts_ns']}")
                decoded += 1
            if decoded != len(rows) or output_rows != rows or output_stamps != stamps:
                raise ValueError(f"{prefix}: proxy frame count/stamp validation failed")
            if any(file_hash(root / p) != digest for p, digest in source_hashes.items()):
                raise ValueError(f"{prefix}: source changed during export")
            session["source_files"] = source_hashes
            session["files"] = [Path(p).relative_to(destination).as_posix()
                                for p in [output.base + ".json", output.csv_path, *output.mkv_paths]]
            session["validation"] = {"ok": True, "decoded_frames": decoded, "stamps_preserved": True}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--profile-root", default=str(Path(__file__).resolve().parents[1] / "export-profiles"))
    args = parser.parse_args(argv)
    try:
        request = json.loads(Path(args.request).read_text())
        result = (preflight if args.preflight else convert)(request, args.profile_root)
        Path(args.result).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    except (ValueError, OSError, KeyError, TypeError, ImportError) as exc:
        print(f"camera export: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
