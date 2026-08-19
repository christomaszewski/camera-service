"""Tests for the reconnect worker's failure handling and the health stall signal.

The reconnect path is the code that exists so a camera flap doesn't end the run -- which makes a bug
in it especially expensive, because it only fires when something has ALREADY gone wrong. The defect
these pin: `source.start()` sat outside the retry's try, and for GigE start() issues Aravis control
writes that raise if the camera drops again between reopen and AcquisitionStart. That escaped the
thread with `_reconnecting` stuck True, and the watchdog gates on that flag -- so capture stopped
permanently, the pipeline stayed PLAYING, and the health line kept printing frozen counters that read
as healthy.

pipeline.py imports gi at module scope, so this SKIPs on a bare host. No GStreamer objects are built.

Run: python3 core-driver/tests/test_pipeline_reconnect.py
"""
import logging
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from cam_driver.pipeline import CapturePipeline
    from cam_driver.sources.base import SourceConfigChanged
except (ImportError, ValueError) as e:
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(f"pipeline needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")
    sys.exit(0)


class _FlakySource:
    """Counts calls and raises on demand, so a test can place the failure precisely."""

    def __init__(self, reopen_fails=0, start_fails=0, stop_raises=False):
        self.reopen_calls = self.start_calls = self.stop_calls = self.close_calls = 0
        self._reopen_fails = reopen_fails
        self._start_fails = start_fails
        self._stop_raises = stop_raises

    def stop(self):
        self.stop_calls += 1
        if self._stop_raises:
            raise RuntimeError("stop blew up")

    def close(self):
        self.close_calls += 1

    def reopen(self):
        self.reopen_calls += 1
        if self.reopen_calls <= self._reopen_fails:
            raise RuntimeError(f"device not back (attempt {self.reopen_calls})")

    def start(self, _on_frame, _on_encoded=None):
        self.start_calls += 1
        if self.start_calls <= self._start_fails:
            # What Aravis raises when the control channel died between reopen and AcquisitionStart.
            raise RuntimeError(f"AcquisitionStart failed (attempt {self.start_calls})")


def _pipe(source):
    cfg = SimpleNamespace(camera=SimpleNamespace(reconnect_backoff_s=0.001,
                                                 reconnect_backoff_max_s=0.002))
    p = CapturePipeline(cfg, source, SimpleNamespace())
    p._reconnecting = True          # the watchdog sets this before spawning the worker
    return p


def test_start_failure_retries_instead_of_killing_the_worker():
    # THE defect: one failed start() used to escape the thread entirely.
    src = _FlakySource(start_fails=2)
    p = _pipe(src)
    p._reconnect()
    assert src.start_calls == 3, "a failed start must be retried, not fatal"
    assert src.reopen_calls == 3, "each retry must re-open the device first"
    assert p._reconnecting is False


def test_reconnecting_flag_is_released_on_success():
    src = _FlakySource()
    p = _pipe(src)
    p._reconnect()
    assert src.start_calls == 1
    assert p._reconnecting is False, "a stuck flag gates the watchdog shut forever"


def test_reconnecting_flag_is_released_on_an_unforeseen_raise():
    # source.stop() runs before the retry loop; nothing catches it. The finally is what keeps an
    # unanticipated failure from being terminal rather than merely noisy.
    src = _FlakySource(stop_raises=True)
    p = _pipe(src)
    try:
        p._reconnect()
    except RuntimeError:
        pass
    assert p._reconnecting is False


def test_stop_during_backoff_ends_the_worker_cleanly():
    src = _FlakySource(reopen_fails=99)
    p = _pipe(src)
    p._stopping = True
    p._stop_event.set()
    p._reconnect()
    assert p._reconnecting is False
    assert src.start_calls == 0


def test_stop_during_reopen_hands_the_device_back():
    # Re-arming after shutdown() released the device would leave a GigE control privilege held by a
    # daemon thread until the camera's heartbeat timeout.
    class _StopsMidReopen(_FlakySource):
        def __init__(self, pipe_ref):
            super().__init__()
            self.pipe_ref = pipe_ref

        def reopen(self):
            super().reopen()
            self.pipe_ref[0]._stopping = True

    ref = []
    src = _StopsMidReopen(ref)
    p = _pipe(src)
    ref.append(p)
    p._reconnect()
    assert src.start_calls == 0, "must not re-arm a source we are dropping"
    assert src.close_calls == 1, "the reacquired device must be handed straight back"
    assert p._reconnecting is False


def test_pipeline_parks_its_stop_event_on_the_source():
    # The seam long reopen() waits poll (the GigE PTP lock poll rides it down to camera.enable_ptp).
    # Identity, not equality: a copy would never see shutdown's set().
    src = _FlakySource()
    p = _pipe(src)
    assert src.stop_event is p._stop_event


def test_stop_interrupts_a_reopen_blocked_on_a_long_wait():
    # The GigE PTP lock poll inside reopen() can run ptp_lock_timeout_s (10s by default) -- LONGER
    # than shutdown's join budget (_RECONNECT_JOIN_S = 5s). The join stays honest only because that
    # wait polls source.stop_event. This pins the contract end-to-end with a fake whose reopen
    # blocks on the event exactly the way camera.enable_ptp does: if the pipeline stops parking the
    # event, or a reopen wait stops polling it, the worker outlives the join and the device is
    # released late -- the control-privilege leak all over again.
    class _BlocksInReopen(_FlakySource):
        def __init__(self):
            super().__init__()
            self.entered_reopen = threading.Event()
            self.wait_interrupted = None

        def reopen(self):
            super().reopen()
            self.entered_reopen.set()
            self.wait_interrupted = self.stop_event.wait(30)   # the stand-in PTP lock poll

    src = _BlocksInReopen()
    p = _pipe(src)
    worker = threading.Thread(target=p._reconnect, daemon=True)
    worker.start()
    assert src.entered_reopen.wait(5), "worker never reached reopen()"
    p._stopping = True       # what request_stop does, in the order it does it
    p._stop_event.set()
    worker.join(5)           # the real budget: _RECONNECT_JOIN_S
    assert not worker.is_alive(), "a stop must interrupt a reopen's long wait, not sit it out"
    assert src.wait_interrupted is True, "the wait must end via the stop event, not its own timeout"
    assert src.close_calls == 1, "the reacquired device must be handed straight back"
    assert src.start_calls == 0, "must not re-arm a source we are dropping"
    assert p._reconnecting is False


def test_source_config_change_is_fatal_and_still_releases_the_flag():
    class _Changed(_FlakySource):
        def reopen(self):
            super().reopen()
            raise SourceConfigChanged("codec changed")

    p = _pipe(_Changed())
    p._reconnect()
    assert p._fatal is True
    assert p._reconnecting is False


# ---- health stall signal ----------------------------------------------------------------

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _health(pipe):
    log = logging.getLogger("cam_driver.pipeline")
    cap = _Capture()
    log.addHandler(cap)
    prev = log.level
    log.setLevel(logging.DEBUG)
    try:
        pipe._log_health()
    finally:
        log.removeHandler(cap)
        log.setLevel(prev)
    return cap.records


def test_health_reports_a_stall_when_frames_stop_advancing():
    p = _pipe(_FlakySource())
    p._reconnecting = False
    p._n_pushed = 42
    p._last_health_frames = 42
    recs = _health(p)
    assert any(r.levelno >= logging.ERROR and "NO frames" in r.getMessage() for r in recs), \
        "a frozen frame count must not print as a healthy line"


def test_health_is_quiet_while_a_reconnect_is_in_flight():
    # The watchdog already said so; a second alarm for a known gap is noise.
    p = _pipe(_FlakySource())
    p._reconnecting = True
    p._n_pushed = 42
    p._last_health_frames = 42
    assert not any("NO frames" in r.getMessage() for r in _health(p))


def test_health_tracks_progress_between_ticks():
    p = _pipe(_FlakySource())
    p._reconnecting = False
    p._n_pushed = 100
    p._last_health_frames = 40
    recs = _health(p)
    assert not any("NO frames" in r.getMessage() for r in recs)
    assert p._last_health_frames == 100, "the tick must record where it got to"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
