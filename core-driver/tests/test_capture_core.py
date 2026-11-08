"""frame-capture pure core (docs/CAPTURE.md): per-frame time normalization, the recent-frames buffer
and nearest-frame selection, trigger parsing, and moving a GPS fix to the chosen frame.

No GStreamer and no zenoh binding. Run: python3 -m pytest core-driver/tests/test_capture_core.py
"""
import json
import math
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.capture import timebase as tb  # noqa: E402
from cam_driver.capture.frames import (NO_FRAME_WITHIN_TOLERANCE, NO_FRAMES, TOO_OLD,  # noqa: E402
                                       Frame, FrameRing)
from cam_driver.capture.request import (GpsFix, RequestError, fix_at,  # noqa: E402
                                        parse_request)

S = 1_000_000_000
MS = 1_000_000
NOW = 1_794_000_000 * S          # a fixed "now" (2026) for the timebase cases
TAI = 37 * S


# ---- timebase ------------------------------------------------------------------------------
def _tbase(**kw):
    return tb.TimeBase(tai_offset_ns=TAI, **kw)


def test_a_utc_stamp_slightly_before_arrival_is_matched_as_captured():
    assert _tbase().normalize(NOW - 30 * MS, NOW) == (NOW - 30 * MS, tb.CAPTURE)


def test_a_tai_stamp_is_recognized_and_the_offset_removed():
    assert _tbase().normalize(NOW - 30 * MS + TAI, NOW) == (NOW - 30 * MS, tb.CAPTURE_TAI)


def test_a_free_running_counter_falls_back_to_plugin_arrival():
    hours_since_power_on = 3 * 3600 * S
    assert _tbase().normalize(hours_since_power_on, NOW) == (NOW, tb.PLUGIN_ARRIVAL)


def test_a_missing_stamp_or_the_arrival_basis_uses_arrival():
    assert _tbase().normalize(0, NOW) == (NOW, tb.PLUGIN_ARRIVAL)
    assert _tbase(basis="arrival").normalize(NOW - MS, NOW) == (NOW, tb.PLUGIN_ARRIVAL)


def test_an_explicit_scale_accepts_only_that_scale():
    assert _tbase(scale="utc").normalize(NOW - MS + TAI, NOW) == (NOW, tb.PLUGIN_ARRIVAL)
    assert _tbase(scale="tai").normalize(NOW - MS, NOW) == (NOW, tb.PLUGIN_ARRIVAL)
    assert _tbase(scale="tai").normalize(NOW - MS + TAI, NOW) == (NOW - MS, tb.CAPTURE_TAI)


def test_playback_and_the_capture_basis_trust_recorded_stamps_far_from_now():
    recorded = NOW - 30 * 86400 * S
    assert _tbase(playback=True).normalize(recorded, NOW) == (recorded, tb.CAPTURE)
    assert _tbase(basis="capture").normalize(recorded, NOW) == (recorded, tb.CAPTURE)
    assert _tbase(basis="capture", scale="tai").normalize(recorded, NOW) == (recorded - TAI, tb.CAPTURE_TAI)


def test_the_latency_budget_bounds_plausibility():
    late = _tbase(max_age_ns=100 * MS)
    assert late.normalize(NOW - 50 * MS, NOW)[1] == tb.CAPTURE
    assert late.normalize(NOW - 500 * MS, NOW)[1] == tb.PLUGIN_ARRIVAL
    assert _tbase().normalize(NOW + 2 * S, NOW)[1] == tb.PLUGIN_ARRIVAL     # from the future


def test_bad_basis_or_scale_is_refused():
    with pytest.raises(ValueError, match="time_basis"):
        tb.TimeBase(basis="gps")
    with pytest.raises(ValueError, match="time_scale"):
        tb.TimeBase(scale="gps")


def test_tai_offset_resolution():
    assert tb.resolve_tai_offset_s(36) == 36
    assert tb.resolve_tai_offset_s("auto") in (tb.kernel_tai_offset_s() or tb.TAI_UTC_DEFAULT_S,)
    assert tb.resolve_tai_offset_s(None) >= 0


# ---- frames --------------------------------------------------------------------------------
def _frame(fid, utc_ns, arrival_ns=None, size=10):
    return Frame(fid, utc_ns, utc_ns, "capture", utc_ns if arrival_ns is None else arrival_ns, None,
                 "GRAY8", 4, 4, None, b"\x00" * size)


def _ring(n=10, step=40 * MS, start=NOW, **kw):
    r = FrameRing(**kw) if kw else FrameRing()
    for i in range(n):
        r.push(_frame(i, start + i * step))
    return r


def test_eviction_keeps_frames_received_within_the_span():
    r = _ring(n=100, span_s=1.0)
    st = r.stats()
    assert st["frames"] == 26 and st["newest_ns"] == NOW + 99 * 40 * MS     # 1 s at 25 fps, inclusive
    assert st["span_ms"] == 1000.0


def test_eviction_is_by_receipt_so_a_looping_replay_does_not_keep_the_previous_lap():
    r = FrameRing(span_s=1.0)
    for i in range(10):
        r.push(_frame(i, NOW + i * 40 * MS, arrival_ns=NOW + i * 40 * MS))
    lap2_start = NOW - 3600 * S                       # stamps jump back an hour at the wrap
    r.push(_frame(100, lap2_start, arrival_ns=NOW + 5 * S))
    assert r.stats()["frames"] == 1 and r.newest().frame_id == 100


def test_eviction_by_bytes_keeps_at_least_the_newest_frame():
    r = FrameRing(span_s=10.0, max_bytes=25)
    for i in range(5):
        r.push(_frame(i, NOW + i * MS, size=10))
    assert r.stats()["frames"] == 2 and r.evicted_for_bytes == 3
    big = FrameRing(span_s=10.0, max_bytes=5)
    big.push(_frame(0, NOW, size=10))
    assert big.stats()["frames"] == 1


def test_nearest_frame_within_tolerance():
    r = _ring()
    f, err = r.select(NOW + 3 * 40 * MS + 15 * MS, 40 * MS, 0)
    assert err is None and f.frame_id == 3
    f, err = r.select(NOW + 3 * 40 * MS + 25 * MS, 40 * MS, 0)
    assert f.frame_id == 4


def test_too_old_and_outside_tolerance_are_refusals():
    r = _ring()
    assert r.select(NOW - S, 40 * MS, 0) == (None, TOO_OLD)
    assert r.select(NOW + 20 * MS, 5 * MS, 0) == (None, NO_FRAME_WITHIN_TOLERANCE)


def test_no_stamp_means_the_newest_frame():
    r = _ring()
    f, err = r.select(None, 0, 0)
    assert err is None and f.frame_id == 9
    assert FrameRing().select(None, 0, 0.01) == (None, NO_FRAMES)


def test_selection_waits_for_a_frame_that_has_not_come_through_yet():
    r = _ring(n=3)
    target = NOW + 5 * 40 * MS

    def late_frames():
        for i in range(3, 7):
            time.sleep(0.02)
            r.push(_frame(i, NOW + i * 40 * MS))

    threading.Thread(target=late_frames, daemon=True).start()
    f, err = r.select(target, 40 * MS, 2.0)
    assert err is None and f.frame_id == 5


def test_a_future_request_gives_up_after_the_wait():
    r = _ring(n=3)
    t0 = time.monotonic()
    assert r.select(NOW + 10 * S, 40 * MS, 0.1) == (None, NO_FRAME_WITHIN_TOLERANCE)
    assert 0.08 <= time.monotonic() - t0 < 1.0


def test_interval_is_the_median_gap():
    assert _ring(n=10).interval_ns() == 40 * MS
    assert FrameRing().interval_ns() is None
    with pytest.raises(ValueError):
        FrameRing(span_s=0)
    with pytest.raises(ValueError):
        FrameRing(max_bytes=0)


# ---- requests ------------------------------------------------------------------------------
def _req(obj):
    return parse_request(json.dumps(obj).encode())


def test_a_full_request_parses():
    r = _req({"v": 1, "stamp_ns": NOW, "tolerance_ms": 30, "wait_ms": 100, "tag": "overlap", "id": 17,
              "gps": {"lat": 40.5, "lon": -79.9, "alt_m": 300, "alt_ref": "MSL", "vel_enu": [1, 2, 0],
                      "h_acc_m": 0.02, "stamp_ns": NOW - MS},
              "orientation": {"yaw_deg": 90, "pitch_deg": -2, "roll_deg": 1}, "meta": {"leg": 3}})
    assert (r.stamp_ns, r.tolerance_ms, r.wait_ms, r.tag, r.id) == (NOW, 30, 100, "overlap", "17")
    assert r.gps.alt_ref == "msl" and r.gps.vel_enu == (1.0, 2.0, 0.0) and r.gps.msl_alt_m() == 300
    assert r.orientation.yaw_deg == 90 and r.meta == {"leg": 3}
    assert r.to_dict()["meta"] == {"leg": 3} and "gps" not in r.to_dict()


def test_an_empty_request_is_a_manual_snapshot_and_selector_parameters_work():
    r = parse_request(None, "")
    assert r.stamp_ns is None and r.tag == "manual"
    r = parse_request(None, f"stamp_ns={NOW};tag=interval;tolerance_ms=20")
    assert (r.stamp_ns, r.tag, r.tolerance_ms) == (NOW, "interval", 20.0)


@pytest.mark.parametrize("payload, match", [
    (b"{not json", "not JSON"),
    (b"[1, 2]", "JSON object"),
    (json.dumps({"v": 2}).encode(), "version"),
    (json.dumps({"stamp_ns": True}).encode(), "stamp_ns"),
    (json.dumps({"stamp_ns": -5}).encode(), "stamp_ns"),
    (json.dumps({"tolerance_ms": "x"}).encode(), "tolerance_ms"),
    (json.dumps({"gps": {"lat": 91, "lon": 0}}).encode(), "gps: 'lat'"),
    (json.dumps({"gps": {"lon": 0}}).encode(), "gps: missing 'lat'"),
    (json.dumps({"gps": {"lat": 0, "lon": 0, "alt_ref": "geoid"}}).encode(), "alt_ref"),
    (json.dumps({"gps": {"lat": 0, "lon": 0, "vel_enu": [1, 2]}}).encode(), "vel_enu"),
    (json.dumps({"gps": {"lat": 0, "lon": 0, "vel_enu": [1, "a", 2]}}).encode(), "vel_enu"),
    (json.dumps({"gps": 5}).encode(), "'gps' must be an object"),
    (json.dumps({"orientation": {"pitch_deg": 100}}).encode(), "pitch_deg"),
    (json.dumps({"orientation": []}).encode(), "'orientation' must be an object"),
    (json.dumps({"meta": [1]}).encode(), "meta"),
    (json.dumps({"tag": ""}).encode(), "tag"),
    (json.dumps({"id": [1]}).encode(), "id"),
    (json.dumps({"tolerance_ms": float("nan")}).encode(), "finite"),
    (b"x" * 20000, "too large"),
])
def test_bad_requests_name_the_field(payload, match):
    with pytest.raises(RequestError, match=match):
        parse_request(payload)


def test_a_fix_is_moved_to_the_frame_along_its_velocity():
    fix = GpsFix(lat=40.0, lon=-80.0, alt_m=100.0, stamp_ns=NOW, vel_enu=(0.0, 10.0, 1.0))
    moved, method, dt_ms = fix_at(fix, NOW + 100 * MS, None)
    assert method == "extrapolated" and dt_ms == 100.0
    north_m = (moved.lat - fix.lat) * math.pi / 180 * 6_364_000     # meridian radius near 40 deg
    assert abs(north_m - 1.0) < 0.01 and moved.lon == pytest.approx(fix.lon)
    assert moved.alt_m == pytest.approx(100.1) and moved.stamp_ns == NOW + 100 * MS
    east = GpsFix(lat=0.0, lon=179.99999, stamp_ns=NOW, vel_enu=(10.0, 0.0, 0.0))
    wrapped, _, _ = fix_at(east, NOW + S, None)
    assert wrapped.lon < -179.9                                     # wrapped across the antimeridian


def test_a_fix_without_velocity_or_too_far_away_is_written_as_given():
    fix = GpsFix(lat=1.0, lon=2.0)
    assert fix_at(fix, NOW, None) == (fix, "as_given", None)        # no time to compare against
    assert fix_at(fix, NOW + 5 * MS, NOW) == (fix, "as_given", 5.0)  # the request's stamp is the fix's
    fast = GpsFix(lat=1.0, lon=2.0, stamp_ns=NOW, vel_enu=(1.0, 1.0, 0.0))
    assert fix_at(fast, NOW + 3 * S, None)[1] == "as_given"


def test_mean_sea_level_altitude_needs_msl_or_a_geoid_separation():
    assert GpsFix(lat=0, lon=0, alt_m=50.0).msl_alt_m() is None
    assert GpsFix(lat=0, lon=0, alt_m=50.0, geoid_sep_m=-30.0).msl_alt_m() == 80.0
    assert GpsFix(lat=0, lon=0, alt_m=50.0, alt_ref="msl").msl_alt_m() == 50.0
    assert GpsFix(lat=0, lon=0).msl_alt_m() is None
