"""Late-window fragment indexing preserves ordinal stamps without decoding the prefix."""
import csv
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from test_replay_source import T0, IV, _source, _pump
from gi.repository import Gst
from cam_driver.sources import replay

W, H, FIRST_PTS = 64, 48, 5_000_000_000


def _pixels(index):
    return bytes([20 + index % 200]) * (W * H)


def _write_part(path, first, count, *, codec="ffv1"):
    encoder = ("avenc_ffv1 threads=1" if codec == "ffv1" else
               f"videoconvert ! video/x-raw,format=I420 ! x264enc bframes={0 if codec == 'h264_p' else 2} "
               "key-int-max=12 speed-preset=ultrafast ! h264parse")
    if codec == "h265_p":
        encoder = ('videoconvert ! video/x-raw,format=I420 ! x265enc '
                   'option-string="bframes=0:keyint=12:frame-threads=1:pools=none:lossless=1" '
                   'speed-preset=ultrafast ! h265parse')
    pipeline = Gst.parse_launch(
        f'appsrc name=src format=time caps="video/x-raw,format=GRAY8,width={W},height={H},framerate=25/1" ! '
        f'{encoder} ! matroskamux ! filesink location="{path}"')
    source = pipeline.get_by_name("src")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for i in range(first, first + count):
            buf = Gst.Buffer.new_wrapped(_pixels(i))
            buf.pts, buf.duration = FIRST_PTS + i * IV, IV
            assert source.emit("push-buffer", buf) == Gst.FlowReturn.OK
        source.emit("end-of-stream")
        msg = pipeline.get_bus().timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
        assert msg is not None and msg.type == Gst.MessageType.EOS, msg.parse_error() if msg else "timeout"
    finally:
        pipeline.set_state(Gst.State.NULL)


def _recording(directory, *, codec="ffv1", parts=4, per_part=12):
    base = directory / "indexed"
    for part in range(parts):
        _write_part(Path(f"{base}-{part:05d}.mkv"), part * per_part, per_part, codec=codec)
    count = parts * per_part
    base.with_suffix(".json").write_text(json.dumps({
        "pixel_format": "GRAY8", "bits_per_pixel": 8, "width": W, "height": H,
        "first_timestamp_ns": T0 + FIRST_PTS, "first_pts_ns": FIRST_PTS,
        "base_timestamp_ns": T0,
        "recording_encoder": "ffv1" if codec == "ffv1" else "x265-lossless" if codec == "h265_p" else "x264",
        "session": {"frames_recorded": count, "segments": parts, "truncated": False, "error": None}}))
    with base.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frame_id", "pts_ns", "timestamp_ns", "source", "chunk_ns", "camera_ns", "system_ns"])
        for i in range(count):
            pts = FIRST_PTS + i * IV
            writer.writerow([100 + i, pts, T0 + pts, "camera", "", T0 + pts + 1, T0 + pts + 2])
    return base


def test_late_window_decodes_only_selected_fragment_and_reuses_index_on_restart(tmp_path):
    _recording(tmp_path)
    src = _source(tmp_path, from_s=39 * IV / 1e9)
    frames, decoded = [], []
    new_stamp = src._new_stamp
    src._new_stamp = lambda buf: (decoded.append(1), new_stamp(buf))[1]
    try:
        assert (src._part_idx, src._idx) == (3, 36)
        src.start(lambda stamp, data: frames.append((stamp, bytes(data))))
        _pump(lambda: src.finished)
        assert not src.finished_error and len(decoded) == 12
        assert [st.frame_id for st, _ in frames] == list(range(139, 148))
        assert [data for _, data in frames] == [_pixels(i) for i in range(39, 48)]
        assert [st.timestamp_ns for st, _ in frames] == [T0 + FIRST_PTS + i * IV for i in range(39, 48)]
        for reason in ("restart", "loop"):
            with patch.object(replay, "_index_fragment", side_effect=AssertionError("cached prefix was scanned again")):
                src._restart(reason=reason)
                _pump(lambda: src.finished)
            assert not src.finished_error
        assert len(decoded) == 36 and len(frames) == 27
        for cycle in range(3):
            current = frames[cycle * 9:(cycle + 1) * 9]
            assert [st.frame_id for st, _ in current] == list(range(139, 148))
            assert [st.timestamp_ns for st, _ in current] == [T0 + FIRST_PTS + (i + 48 * cycle) * IV for i in range(39, 48)]
    finally:
        src.stop()


def test_changed_fragment_invalidates_cached_boundary(tmp_path):
    base = _recording(tmp_path)
    src = _source(tmp_path, from_s=39 * IV / 1e9)
    try:
        path = Path(f"{base}-00000.mkv")
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with patch.object(replay, "_index_fragment", wraps=replay._index_fragment) as scan:
            assert src._window_start(0) == (3, 36)
            assert scan.call_count >= 3
        csv_path = base.with_suffix(".csv")
        csv_path.write_text(csv_path.read_text() + "\n")
        with pytest.raises(ValueError, match="changed after loading"):
            src._window_start(0)
    finally:
        src.stop()


def test_missing_prefix_packet_refuses_untrustworthy_csv_offset(tmp_path):
    base = _recording(tmp_path)
    _write_part(Path(f"{base}-00000.mkv"), 0, 11)   # CSV still attests 12 before fragment 1
    with pytest.raises(ValueError, match="compressed packets.*CSV rows"):
        _source(tmp_path, from_s=39 * IV / 1e9)


def test_missing_first_packet_cannot_be_mistaken_for_mux_offset(tmp_path):
    base = _recording(tmp_path)
    _write_part(Path(f"{base}-00000.mkv"), 1, 11)
    # Every surviving PTS appears to match CSV under a constant +one-frame offset.
    # The window ends before EOS, so only a full compressed count exposes the loss.
    with pytest.raises(ValueError, match="compressed packets.*CSV rows"):
        _source(tmp_path, from_s=39 * IV / 1e9, to_s=43 * IV / 1e9)


def test_early_window_does_not_scan_later_fragments(tmp_path):
    _recording(tmp_path, parts=20)
    # Past fragment zero, but near the start of a much longer archive. The cost
    # guard must avoid reading the full archive merely to avoid this short prefix.
    with patch.object(replay, "_index_fragment", side_effect=AssertionError("early window scanned archive")):
        src = _source(tmp_path, from_s=13 * IV / 1e9, to_s=15 * IV / 1e9)
    frames = []
    try:
        assert (src._part_idx, src._idx) == (0, 0)
        src.start(lambda stamp, data: frames.append(stamp.frame_id))
        _pump(lambda: src.finished)
        assert frames == [113, 114] and not src.finished_error
    finally:
        src.stop()


def test_window_in_large_first_fragment_does_not_scan_tail(tmp_path):
    base = _recording(tmp_path)
    _write_part(Path(f"{base}-00000.mkv"), 0, 36)
    for part in range(1, 4):
        _write_part(Path(f"{base}-{part:05d}.mkv"), 36 + (part - 1) * 4, 4)
    with patch.object(replay, "_index_fragment", wraps=replay._index_fragment) as scan:
        src = _source(tmp_path, from_s=25 * IV / 1e9)
    try:
        assert (src._part_idx, src._idx) == (0, 0)
        assert scan.call_count == 1
        assert scan.call_args.args[0] == str(base) + "-00000.mkv"
        assert scan.call_args.kwargs["max_packets"] == 26
    finally:
        src.stop()


def test_b_frames_fall_back_to_presentation_order_decode(tmp_path):
    _recording(tmp_path, codec="h264")
    with patch.object(replay, "_index_fragment", wraps=replay._index_fragment) as scan:
        src = _source(tmp_path, from_s=39 * IV / 1e9)
    assert scan.call_count == 1, "an unsupported layout must not scan the remaining archive"
    frames = []
    try:
        assert (src._part_idx, src._idx) == (0, 0), "demux order must not be assumed to be presentation order"
        src.start(lambda st, data: frames.append(st.frame_id))
        _pump(lambda: src.finished)
        assert frames == list(range(139, 148)) and not src.finished_error
    finally:
        src.stop()


@pytest.mark.parametrize("codec", ["h264_p", "h265_p"])
def test_p_only_codec_index_matches_sequential_decoded_window(tmp_path, codec):
    _recording(tmp_path, codec=codec)
    reference = []
    src = _source(tmp_path)
    try:
        src.start(lambda st, data: reference.append((st, bytes(data))))
        _pump(lambda: src.finished)
        assert not src.finished_error
        src._restart()
        _pump(lambda: src.finished)
        assert not src.finished_error and len(reference) == 96
    finally:
        src.stop()
    src = _source(tmp_path, from_s=39 * IV / 1e9)
    frames = []
    try:
        assert (src._part_idx, src._idx) == (3, 36)
        src.start(lambda st, data: frames.append((st, bytes(data))))
        _pump(lambda: src.finished)
        assert not src.finished_error and frames == reference[39:48]
        with patch.object(replay, "_index_fragment", side_effect=AssertionError("cached index was scanned again")):
            src._restart()
            _pump(lambda: src.finished)
        assert not src.finished_error and frames == reference[39:48] + reference[87:96]
    finally:
        src.stop()


def test_fragment_index_keeps_fractional_pts_and_recorded_gaps(tmp_path):
    from test_proxy_export_gst import _write_segmented_session
    _, points = _write_segmented_session(tmp_path, "GRAY8")
    src = _source(tmp_path, epoch_unix_ns=T0, from_s=points[73] / 1e9)
    frames = []
    try:
        assert src._part_idx > 0 and src._idx <= 73
        src.start(lambda st, data: frames.append((st, bytes(data))))
        _pump(lambda: src.finished)
        assert not src.finished_error
        assert [st.frame_id for st, _ in frames] == list(range(163, 190))
        assert [st.timestamp_ns for st, _ in frames] == [T0 + point for point in points[73:]]
        assert [data for _, data in frames] == [bytes([(i * 20) % 256]) * 256 for i in range(73, 100)]
    finally:
        src.stop()
