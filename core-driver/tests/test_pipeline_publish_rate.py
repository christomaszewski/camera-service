"""Tests for the plugin-endpoint publish-rate cap (transport.plugin_endpoint.max_rate_hz), the
decimator behind CapturePipeline._should_publish.

With a KNOWN source rate the cap is every-Nth-frame decimation (N = ceil(fps / cap)): the only
decimation with even spacing. The old timestamp gate (`ts - last >= 1/cap`) is kept only for a
source whose rate is unknown -- on a 24 fps source capped at 10 Hz it let frames through on
alternating 2- and 3-frame gaps (91 / 136 ms) whenever arrival jitter nudged a pair over the 100 ms
threshold, which a browser renders as judder at a nominal '10 fps'. The pipeline is never built: only
the pure decision method runs. pipeline.py imports gi at module scope, so this SKIPs on a bare host.

Run: python3 core-driver/tests/test_pipeline_publish_rate.py
"""
import os
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cam_driver.pipeline import CapturePipeline
except (ImportError, ValueError) as e:   # no gi/GStreamer on this host
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(f"pipeline needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")
    sys.exit(0)

NS = 1_000_000_000


def _pipe(max_rate_hz, fps):
    cfg = SimpleNamespace(camera=SimpleNamespace(frame_rate=fps),
                          transport=SimpleNamespace(plugin_endpoint=SimpleNamespace(max_rate_hz=max_rate_hz)))
    p = CapturePipeline(cfg, source=SimpleNamespace())   # the ctor parks its stop event on the source
    p._fps = fps
    return p


def _arrivals(fps, n, jitter_ns=0, seed=1):
    """Capture timestamps of n frames at fps, each displaced by a seeded-random amount within
    +-jitter_ns (GVSP / decoder wobble) -- a real source never hits its nominal interval exactly."""
    rng = random.Random(seed)
    iv = NS / fps
    return [int(i * iv + (rng.uniform(-jitter_ns, jitter_ns) if jitter_ns else 0)) for i in range(n)]


def _published(p, stamps):
    return [i for i, ts in enumerate(stamps) if p._should_publish(ts)]


def test_no_cap_publishes_every_frame():
    p = _pipe(0.0, 24.0)
    assert p._publish_every_n() == 0
    assert _published(p, _arrivals(24, 10)) == list(range(10))


def test_known_rate_is_even_integer_decimation_that_never_exceeds_the_cap():
    # 24 fps capped at 10 Hz: ceil(2.4) = 3 -> every 3rd frame, 8 Hz, evenly spaced
    p = _pipe(10.0, 24.0)
    assert p._publish_every_n() == 3
    assert _published(p, _arrivals(24, 12)) == [0, 3, 6, 9]
    # 24 fps capped at 12 Hz divides: every 2nd frame, exactly 12 Hz
    p = _pipe(12.0, 24.0)
    assert p._publish_every_n() == 2
    assert _published(p, _arrivals(24, 12)) == [0, 2, 4, 6, 8, 10]
    # a cap at or above the source rate is a no-op
    assert _pipe(30.0, 24.0)._publish_every_n() == 1
    assert _published(_pipe(30.0, 24.0), _arrivals(24, 5)) == [0, 1, 2, 3, 4]
    # an exact quotient must not be pushed up by float error (30 / 10 == 3, not ceil(3.0000001) = 4)
    assert _pipe(10.0, 30.0)._publish_every_n() == 3
    assert _pipe(10.0, 20.0)._publish_every_n() == 2


def test_decimation_ignores_arrival_jitter():
    # +-10 ms of jitter on a 22 fps source (the flight-2 bag's real delivered rate: 2 frames = 91 ms,
    # 9 ms from the 100 ms gate): the every-Nth decimator is indifferent to when the frames arrived
    p = _pipe(10.0, 22.0)
    assert _published(p, _arrivals(22, 24, jitter_ns=10_000_000)) == [0, 3, 6, 9, 12, 15, 18, 21]


def test_unknown_rate_falls_back_to_the_timestamp_gate():
    p = _pipe(10.0, 0.0)
    assert p._publish_every_n() == 0
    stamps = _arrivals(24, 12)                     # 41.7 ms apart, 100 ms gate -> every 3rd on the nose
    assert _published(p, stamps) == [0, 3, 6, 9]
    # ... and the gate's known unevenness under jitter is exactly why the decimator exists: the same
    # jittered 22 fps source the decimator handled evenly comes out as mixed 2- and 3-frame gaps
    p = _pipe(10.0, 0.0)
    got = _published(p, _arrivals(22, 48, jitter_ns=10_000_000))
    gaps = {b - a for a, b in zip(got, got[1:])}
    assert gaps == {2, 3}, got


def test_timestamp_gate_reopens_on_a_backward_clock_step():
    p = _pipe(10.0, 0.0)
    assert p._should_publish(5 * NS)
    assert not p._should_publish(5 * NS + 10_000_000)
    assert p._should_publish(1 * NS)                # source clock reset (reconnect): publish, don't wait it out


def test_reconnect_reset_restarts_the_decimation_phase():
    p = _pipe(10.0, 24.0)
    assert _published(p, _arrivals(24, 4)) == [0, 3]
    p._pub_seq = 0                                  # what the reconnect path does alongside _last_pub_ts
    assert p._should_publish(0)                     # the reopened source's first frame goes out at once


def test_build_log_note_names_the_effective_rate():
    assert _pipe(10.0, 24.0)._publish_rate_note() == " -> every 3rd frame (8.0 Hz)"
    assert _pipe(12.0, 24.0)._publish_rate_note() == " -> every 2nd frame (12.0 Hz)"
    assert _pipe(30.0, 24.0)._publish_rate_note() == " -> every frame (24.0 Hz)"
    assert _pipe(5.0, 24.0)._publish_rate_note() == " -> every 5th frame (4.8 Hz)"
    assert _pipe(10.0, 0.0)._publish_rate_note() == ""
    assert _pipe(0.0, 24.0)._publish_rate_note() == ""


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
