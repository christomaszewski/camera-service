"""ROS2-source conversion overload tests with real GStreamer, without a ROS installation.

Run in cam-dev on GStreamer 1.20 and a modern image:
    python3 -m pytest core-driver/tests/test_ros2_source_feeder.py
"""
import sys
import threading
from pathlib import Path

import gi
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins" / "ros2-source"))
from cam_ros2_source.node import CONVERT_QUEUE_FRAMES, Feeder, Settings  # noqa: E402

Gst.init(None)
WIDTH, HEIGHT = 4, 4
STAMP = 1_800_000_000_000_000_000
PERIOD = 33_333_333


class CollectingFeeder(Feeder):
    """Exercise the real conversion graph; collect its output before socket framing."""

    def __init__(self):
        super().__init__(Settings({"CAM_SOURCE_TOPIC": "/test/image", "CAM_SOURCE_FORMAT": "RGB",
                                   "CAM_SOURCE_WIDTH": str(WIDTH), "CAM_SOURCE_HEIGHT": str(HEIGHT),
                                   "CAM_SOURCE_FPS": "30"}))
        self.received = []
        self.condition = threading.Condition()

    def _emit(self, pixels, stamp):
        with self.condition:
            self.received.append((pixels, stamp))
            self.condition.notify_all()

    def wait_for_frames(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.received) >= count, timeout=5), self.received


@pytest.fixture
def feeder():
    feeder = CollectingFeeder()
    try:
        yield feeder
    finally:
        feeder.stop()


def _caps(width=WIDTH):
    return f"video/x-raw,format=BGR,width={width},height={HEIGHT},framerate=30/1"


def _push(feeder, value, width=WIDTH):
    pixels = bytes((value, value + 1, value + 2)) * (width * HEIGHT)
    feeder.push_image("bgr8", width, HEIGHT, False, width * 3, pixels, STAMP + value * PERIOD)


def _expected(value):
    return bytes((value + 2, value + 1, value)) * (WIDTH * HEIGHT), STAMP + value * PERIOD


def test_conversion_overload_keeps_newest_frames_before_conversion(feeder):
    feeder._ensure_convert(_caps(), "")
    entered, release, timed_out = threading.Event(), threading.Event(), threading.Event()
    pad = feeder.cv_in.get_static_pad("src")

    def hold_first(_pad, _info):
        if not entered.is_set():
            entered.set()
            if not release.wait(5):
                timed_out.set()
        return Gst.PadProbeReturn.OK

    probe = pad.add_probe(Gst.PadProbeType.BUFFER, hold_first)
    try:
        _push(feeder, 0)
        assert entered.wait(5), "first frame never reached the converter"
        count = 30
        for value in range(1, count):
            _push(feeder, value)
            assert feeder.cv_in.get_property("current-level-buffers") <= CONVERT_QUEUE_FRAMES
        assert feeder.cv_in.get_property("current-level-buffers") == 2
        assert feeder.cv_in.get_property("current-level-bytes") == 2 * WIDTH * HEIGHT * 3
        release.set()
        feeder.wait_for_frames(3)
        assert not timed_out.is_set(), "the overload burst blocked the producer"
        assert feeder.received == [_expected(value) for value in (0, count - 2, count - 1)]
        assert feeder._dropped == count - 3
        feeder.stop()
        assert feeder._dropped == count - 3, "stop recounted already observed queue leaks"
    finally:
        release.set()
        pad.remove_probe(probe)


def test_rebuild_accounts_for_discarded_tail_and_resets_conversion_sequence(feeder):
    feeder._ensure_convert(_caps(), "")
    # A live appsrc in PAUSED does not stream. Going via READY first ensures no old create()
    # call is waiting for input, so this is a deterministic queued tail, without timing sleeps.
    feeder.cv_pipeline.set_state(Gst.State.READY)
    feeder.cv_pipeline.set_state(Gst.State.PAUSED)
    assert feeder.cv_pipeline.get_state(Gst.SECOND)[1] == Gst.State.PAUSED
    count = 7
    for value in range(count):
        _push(feeder, value)
    assert feeder.received == []
    assert feeder.cv_in.get_property("current-level-buffers") == 2
    assert feeder.cv_in.emit("end-of-stream") == Gst.FlowReturn.OK
    _push(feeder, count)
    assert feeder._dropped == 1, "a rejected push is counted immediately"

    # A geometry change tears down the old graph, including leaked frames whose sequence gap
    # has not reached the src-pad probe yet and the two frames still waiting in appsrc. The
    # rejected post-EOS push must not also be counted as an unfinished conversion.
    _push(feeder, count + 1, width=WIDTH * 2)
    feeder.wait_for_frames(1)
    assert feeder.received == [_expected(count + 1)]
    assert feeder._dropped == count + 1
    assert feeder._cv_submitted == feeder._cv_last_forwarded == 1
    feeder.stop()
    feeder.stop()
    assert feeder._dropped == count + 1, "cleanup must not count the old tail twice"
