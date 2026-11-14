"""Compact replay/export metadata must preserve all stamp values and CSV validation."""
import csv
import gc
from pathlib import Path
import sys
import tracemalloc
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cam_driver.export_recordings import _rows
from cam_driver.playback import FrameStamps, load_stamps, median_interval_ns
from cam_driver.timestamps import FrameStamp, TimestampSource

FIELDS = ["frame_id", "pts_ns", "timestamp_ns", "source", "chunk_ns", "camera_ns", "system_ns"]


def _write(path, rows, fields=FIELDS):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)


def test_compact_stamps_preserve_provenance_nulls_and_integer_range(tmp_path):
    path = tmp_path / "capture.csv"
    sources = list(TimestampSource)
    rows, expected = [], []
    # uint64 frame IDs, signed values and values beyond uint64 retain old int() semantics.
    for i, value in enumerate((0, -1, 2**63 - 1, 2**63, 2**64 - 1, 2**80, -(2**80))):
        source = sources[i % len(sources)]
        chunk = None if i % 2 else value
        rows.append((value, i, value, source.value, "" if chunk is None else chunk, value, value))
        expected.append(FrameStamp(value, value, source, value, value, chunk))
    rows.append((10, 10, 10, "future-timestamp-source", "", "", ""))
    expected.append(FrameStamp(10, 10, TimestampSource.SYSTEM, 0, 0, None))
    _write(path, rows)
    stamps = load_stamps(path)
    assert isinstance(stamps, FrameStamps)
    assert stamps == expected and expected == stamps
    assert stamps == load_stamps(path)
    assert list(stamps) == expected
    assert stamps[-1] == expected[-1]
    assert stamps[::-2] == expected[::-2]
    assert stamps.timestamps[-2] == -(2**80)
    assert stamps.frame_ids[3:6] == [row.frame_id for row in expected[3:6]]
    for index in (len(stamps), -len(stamps) - 1):
        with pytest.raises(IndexError):
            stamps[index]
    stamps[0].timestamp_ns = 99
    assert stamps[0].timestamp_ns == 0   # consumers receive independent objects


def test_export_metadata_keeps_exact_pts_and_extra_named_fields_on_disk(tmp_path):
    path = tmp_path / "capture.csv"
    _write(path, [(91, "0000", 1000, "camera", "", 1001, 1005, "line one\nline two, quoted"),
                  (92, "0040", 1040, "ptp_chunk", 1040, 1041, 1045, "")], FIELDS + ["annotation"])
    original = path.read_bytes()
    fields, pts, stamps = _rows(SimpleNamespace(csv_path=path, header={"session": {"frames_recorded": 2}}))
    assert fields == FIELDS + ["annotation"]
    assert pts == [0, 40]
    assert stamps.timestamps == [1000, 1040]
    assert median_interval_ns(stamps.timestamps) == 40
    assert path.read_bytes() == original


@pytest.mark.parametrize("rows,summary,fields,error", [
    ([], {}, FIELDS, "nonempty"),
    ([(1, 0, 100, "system", "", 100, 100)], {"frames_recorded": 2}, FIELDS, "frame count"),
    ([(1, -1, 100, "system", "", 100, 100)], {}, FIELDS, "PTS"),
    ([(1, 0, -1, "system", "", 100, 100)], {}, FIELDS, "capture timestamps"),
    ([(1, 0, 100, "system", "", 100, 100), (2, 1, 99, "system", "", 100, 100)], {}, FIELDS, "capture timestamps"),
    ([(1, 0, 100, "system", "", 100, 100), (2, 0, 100, "system", "", 100, 100)], {}, FIELDS, "PTS"),
    ([(1, 0, 100, "system", "", 100, 100, "extra")], {}, FIELDS, "unlabelled"),
    ([(1, 0, 100, "system", "", 100, 100)], {}, ["frame_id", "other", *FIELDS[2:]], "pts_ns"),
])
def test_export_metadata_keeps_integrity_checks(tmp_path, rows, summary, fields, error):
    path = tmp_path / "capture.csv"
    _write(path, rows, fields)
    with pytest.raises(ValueError, match=error):
        _rows(SimpleNamespace(csv_path=path, header={"session": summary}))


def test_long_metadata_does_not_retain_one_dict_per_frame(tmp_path):
    path = tmp_path / "capture.csv"
    frames, start = 20_000, 1_800_000_000_000_000_000
    _write(path, ((i, i * 40_000_000, start + i * 40_000_000, "ptp_chunk",
                   start + i * 40_000_000, start + i * 40_000_000 + 1,
                   start + i * 40_000_000 + 2) for i in range(frames)))
    gc.collect()
    tracemalloc.start()
    try:
        _, pts, stamps = _rows(SimpleNamespace(csv_path=path, header={}))
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(stamps) == len(pts) == frames
    # Allow substantial allocator/version headroom around 50 bytes/row of columns.
    # The old dictionary/string + FrameStamp representation exceeds 1 KB/row.
    assert retained < frames * 100, retained
    assert peak < frames * 150, peak
