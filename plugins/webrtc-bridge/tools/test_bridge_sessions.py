"""The launcher's glue around viewer sessions, on real GStreamer with a stand-in for webrtcsink.

The three policies are unit-tested on their own (test_session_reaper, test_idle_gate,
test_shared_encoder). This is what ties them to a pipeline in bridge_stream: the session check, the
probe that holds frames back, the shared encoder's caps and keyframes. None of it needs webrtcsink --
a fake sink answers `get-sessions` / `stats` / `signaller`, a fakesink ends the real pipelines -- so it
runs in the hardware-free suite; webrtc_test.sh covers the same paths against the real element.

Run in cam-dev.
"""
import contextlib
import logging
import os
import sys
import time
from unittest.mock import patch

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_stream  # noqa: E402
import shared_encoder  # noqa: E402
from idle_gate import IdleGate  # noqa: E402
from session_reaper import flatten_stats, receiver_key  # noqa: E402

Gst.init(None)
W, H = 64, 48


# ---- helpers -----------------------------------------------------------------------------------------
@contextlib.contextmanager
def _bridge(pipeline=None, **env):
    """A Bridge with `env` (and nothing else of CAM_* from the outside), built on `pipeline` when one is
    given; stopped afterwards."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("CAM_", "VIDEO_CAPS", "GST_PLUGIN_FEATURE_RANK"))}
    clean.update({"CAM_ADVERTISE": "0", "CAM_WEBRTC_STATUS": "0"}, **env)
    if pipeline:
        clean["CAM_PIPELINE"] = pipeline
    with patch.dict(os.environ, clean, clear=True):
        b = bridge_stream.Bridge()
        if pipeline:
            assert b.build()
        try:
            yield b
        finally:
            b._stopping = True                          # every timer of this bridge retires itself
            if b.pipeline is not None:
                b.pipeline.set_state(Gst.State.NULL)
                b.pipeline.get_bus().remove_signal_watch()


def _pump(until, timeout=15.0):
    """Run the default main context (the launcher's timers) until `until()` holds."""
    ctx, deadline = GLib.MainContext.default(), time.monotonic() + timeout
    while not until():
        if time.monotonic() > deadline:
            return False
        ctx.iteration(False)
        time.sleep(0.005)
    return True


class _Signaller:
    def __init__(self, with_client_id=True):
        self.client_id, self.emitted, self.broken = "", [], ()
        self._props = ("client-id",) if with_client_id else ()

    def find_property(self, name):
        return object() if name in self._props else None

    def get_property(self, name):
        assert name == "client-id"
        return self.client_id

    def emit(self, signal, session_id):
        if signal in self.broken:
            raise TypeError("unknown signal " + signal)
        self.emitted.append((signal, session_id))


class _Sink:
    """What bridge_stream asks of webrtcsink for its session check."""

    def __init__(self, signaller=None):
        self.sessions, self.stats, self.signaller = [], Gst.Structure.new_empty("stats"), signaller or _Signaller()

    def emit(self, signal):
        assert signal == "get-sessions"
        return list(self.sessions)

    def get_property(self, name):
        return {"stats": self.stats, "signaller": self.signaller}[name]

    def talk(self, **sessions):
        """Statistics shaped like webrtcsink's: per session, the candidate pair with its transport-cc
        feedback, the outbound stream (the sender alone), and the remote-inbound block of the receiver's
        reports. `sessions` maps an id to (packets the receiver acknowledged, packets sent)."""
        self.stats = Gst.Structure.new_empty("stats")
        for sid, (acked, sent) in sessions.items():
            twcc = Gst.Structure.new_empty("gst-twcc-stats")
            twcc.set_value("packets-recv", acked)
            pair = Gst.Structure.new_empty("candidate-pair")
            pair.set_value("timestamp", time.monotonic())
            pair.set_value("gst-twcc-stats", twcc)
            out = Gst.Structure.new_empty("outbound")
            out.set_value("timestamp", time.monotonic())
            out.set_value("packets-sent", sent)
            rsrc = Gst.Structure.new_empty("gst-rtpsource-stats")
            rsrc.set_value("rb-exthighestseq", acked)
            remote = Gst.Structure.new_empty("remote-inbound")
            remote.set_value("round-trip-time", 0.002)
            remote.set_value("gst-rtpsource-stats", rsrc)
            session = Gst.Structure.new_empty("session")
            session.set_value("ice-candidate-pair_0", pair)
            session.set_value("rtp-outbound-stream-stats_1", out)
            session.set_value("rtp-remote-inbound-stream-stats_1", remote)
            self.stats.set_value(sid, session)


@contextlib.contextmanager
def _reaper_bridge(**env):
    """(bridge, fake sink) with a 0.2 s liveness threshold, so a test waits fractions of a second."""
    with _bridge(CAM_WEBRTC_STALE_S="0.2", **env) as b:
        b._sink = _Sink()
        yield b, b._sink


def _look(b, frames=1):
    """One session check, `frames` having arrived from the camera since the last."""
    b._rx_frames += frames
    b._look_at_sessions()


# ---- the statistics, as GStreamer hands them over ------------------------------------------------------
def test_flatten_and_receiver_key_read_real_gst_structures():
    sink = _Sink()
    sink.talk(s1=(5, 100))
    flat = flatten_stats(sink.stats.get_value("s1"))
    assert flat["ice-candidate-pair_0/gst-twcc-stats/packets-recv"] == "5"
    assert flat["rtp-outbound-stream-stats_1/packets-sent"] == "100"
    assert flat["rtp-remote-inbound-stream-stats_1/gst-rtpsource-stats/rb-exthighestseq"] == "5"
    assert not any(k.endswith("timestamp") for k in flat)
    alive = receiver_key(flat)
    assert len(alive) == 3                              # transport-cc, the report block, the round trip
    sink.talk(s1=(5, 900))                              # the sender alone, into a dead link
    assert receiver_key(flatten_stats(sink.stats.get_value("s1"))) == alive
    sink.talk(s1=(6, 900))                              # the receiver answering
    assert receiver_key(flatten_stats(sink.stats.get_value("s1"))) != alive


def test_the_inventory_is_read_from_the_registry_with_its_ranks():
    have = shared_encoder._available()
    assert set(have) <= {"nvvidconv", "nvv4l2h264enc", "nvv4l2h265enc", "x264enc", "h264parse", "h265parse", "capssetter"}
    assert {"x264enc", "h264parse", "capssetter"} <= set(have)          # the images this suite runs in
    assert have["x264enc"] == int(Gst.ElementFactory.find("x264enc").get_rank())
    assert shared_encoder.select_encoder("h264", have) in ("x264enc", "nvv4l2h264enc")


# ---- the session check ---------------------------------------------------------------------------------
def test_the_gate_stays_open_until_the_sink_has_registered_then_follows_the_sessions():
    with _bridge(CAM_WEBRTC_IDLE_FPS="0", CAM_WEBRTC_IDLE_GRACE_S="0") as b:
        b._sink = sink = _Sink()
        b._gate.await_ready()                           # what _setup_sessions does for a real signaller
        ok, drop = Gst.PadProbeReturn.OK, Gst.PadProbeReturn.DROP
        assert b._on_ingress(None, None) == ok          # codec discovery runs on real frames
        _look(b)
        assert not b._sink_ready and b._on_ingress(None, None) == ok
        sink.signaller.client_id = "2d0a6c1e"           # the signalling server answered: discovery is over
        _look(b)
        assert b._sink_ready and b._on_ingress(None, None) == drop      # nobody watching, no grace
        b._on_consumer_added(None, "peer", None)        # the signal opens it at once...
        assert b._consumers == 1 and b._on_ingress(None, None) == ok
        sink.sessions = ["s1"]
        sink.talk(s1=(1, 1))
        _look(b)                                        # ...and the check agrees
        assert b._consumers == 1 and b._on_ingress(None, None) == ok
        b._on_consumer_removed(None, "peer", None)
        assert b._consumers == 0 and b._on_ingress(None, None) == drop
        b._consumers = 3                                # a count that drifted: a missed signal
        sink.sessions = []
        _look(b)
        assert b._consumers == 0 and b._on_ingress(None, None) == drop
        assert b._gate.dropped == 3


def test_a_session_is_itself_proof_that_the_sink_is_ready():
    with _bridge() as b:
        b._sink = sink = _Sink(_Signaller(with_client_id=False))
        assert b._signaller_property("client-id") is None
        _look(b)
        assert not b._sink_ready
        sink.sessions = ["s1"]
        _look(b)
        assert b._sink_ready


def test_with_a_signaller_that_reports_registration_the_gate_waits_for_it_instead_of_a_fixed_warm_up():
    with _bridge(f"{_SOURCE} ! queue name=ingress_q ! fakesink name=cam_webrtcsink sync=false",
                 CAM_WEBRTC_IDLE_FPS="0", CAM_WEBRTC_IDLE_GRACE_S="0") as b:
        b._gate = IdleGate(idle_fps=0, grace_s=0, warmup_s=0)            # a warm-up that is already over
        b._sink = sink = _Sink()                        # ...but this sink's signaller has a client-id to wait for
        b._setup_sessions()
        assert b._gate.enabled
        assert b._on_ingress(None, None) == Gst.PadProbeReturn.OK        # not registered: discovery is running
        sink.signaller.client_id = "f3a1"
        _look(b)
        assert b._on_ingress(None, None) == Gst.PadProbeReturn.DROP


def test_with_both_policies_off_the_session_check_only_keeps_the_count():
    class _NoStats(_Sink):
        def get_property(self, name):
            assert name == "signaller", "statistics are not read when nothing would be done with them"
            return self.signaller

    with _bridge(CAM_WEBRTC_STALE_S="off") as b:
        b._sink = sink = _NoStats()
        assert not b._reaper.enabled
        sink.sessions = ["a", "b"]
        _look(b)
        assert b._consumers == 2 and sink.signaller.emitted == []


def test_a_silent_viewer_is_ended_with_both_signals_in_order():
    with _reaper_bridge() as (b, sink):
        sink.sessions = ["live", "gone"]
        for n in range(1, 4):                           # both talk
            sink.talk(live=(n, n), gone=(n, n))
            _look(b)
            time.sleep(0.02)
        assert sink.signaller.emitted == []
        deadline = time.monotonic() + 5
        n = 3
        while not sink.signaller.emitted and time.monotonic() < deadline:
            n += 1
            sink.talk(live=(n, n), gone=(3, n))         # `gone` is still sent to, and says nothing
            _look(b)
            time.sleep(0.05)
        # end-session tells the far side; session-ended is what removes the session from the sink
        assert sink.signaller.emitted == [("end-session", "gone"), ("session-ended", "gone")]


def test_a_stalled_camera_is_not_held_against_a_live_viewer():
    with _reaper_bridge() as (b, sink):
        sink.sessions = ["s1"]
        for n in range(1, 4):
            sink.talk(s1=(n, n))
            _look(b)
        for _ in range(10):                             # half a second without a frame, far past the threshold:
            time.sleep(0.05)                            # the receiver has nothing to acknowledge
            _look(b, frames=0)
        assert sink.signaller.emitted == []
        deadline = time.monotonic() + 5                 # frames again, and still no answer: now it counts
        while not sink.signaller.emitted and time.monotonic() < deadline:
            time.sleep(0.05)
            _look(b)
        assert [s for s, _ in sink.signaller.emitted] == ["end-session", "session-ended"]


def test_the_cap_ends_the_oldest_session():
    with _bridge(CAM_WEBRTC_STALE_S="0", CAM_WEBRTC_MAX_SESSIONS="2") as b:
        b._sink = sink = _Sink()
        for sessions in (["a"], ["a", "b"]):
            sink.sessions = sessions
            _look(b)
        assert sink.signaller.emitted == []
        sink.sessions = ["a", "b", "c"]
        _look(b)
        assert sink.signaller.emitted == [("end-session", "a"), ("session-ended", "a")]


def test_statistics_with_no_receiver_in_them_end_nobody_and_the_log_says_why(caplog):
    with _reaper_bridge() as (b, sink), caplog.at_level(logging.WARNING, logger="bridge_stream"):
        sink.sessions = ["s1", "s2"]                    # sessions the sink has no (or foreign) statistics for
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            _look(b)
            time.sleep(0.05)
        assert sink.signaller.emitted == []
        said = [r for r in caplog.records if "no receiver feedback has been seen" in r.getMessage()]
        assert len(said) == 1                           # once, not every two seconds


def test_a_failing_session_check_never_reaches_the_video_path(caplog):
    class _Broken:
        def emit(self, _signal):
            raise RuntimeError("the sink is gone")

    with _bridge() as b, caplog.at_level(logging.WARNING, logger="bridge_stream"):
        b._sink = _Broken()
        assert b._session_tick() is True and b._session_tick() is True     # the timer keeps running
        assert len([r for r in caplog.records if "session check failed" in r.getMessage()]) == 1
        b._stopping = True
        assert b._session_tick() is False


def test_ending_a_session_survives_a_signal_this_build_does_not_have(caplog):
    with _bridge() as b, caplog.at_level(logging.WARNING, logger="bridge_stream"):
        b._sink = sink = _Sink()
        sink.signaller.broken = ("end-session",)
        b._end_session("s1", "a test")
        assert sink.signaller.emitted == [("session-ended", "s1")]
        assert any("could not emit end-session" in r.getMessage() for r in caplog.records)


# ---- the gate, on a pipeline -----------------------------------------------------------------------------
_SOURCE = f"videotestsrc name=cam_src is-live=true ! video/x-raw,format=I420,width={W},height={H},framerate=50/1"


def _count_buffers(pipeline, name="cam_webrtcsink"):
    seen = []
    sink = pipeline.get_by_name(name)
    sink.set_property("signal-handoffs", True)
    sink.connect("handoff", lambda *_a: seen.append(1))
    return seen


def test_frames_are_held_at_the_ingress_queue_until_somebody_watches():
    with _bridge(f"{_SOURCE} ! queue name=ingress_q ! fakesink name=cam_webrtcsink sync=false") as b:
        assert b._gate.enabled
        b._gate = IdleGate(idle_fps=0, grace_s=0, warmup_s=0)             # idle from the first frame
        seen = _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        assert _pump(lambda: b._gate.dropped >= 20)
        assert seen == [] and b._rx_frames >= 20        # the camera delivers; nothing is prepared
        b._gate.sessions(1, time.monotonic())           # a viewer
        assert _pump(lambda: len(seen) >= 20)
        dropped = b._gate.dropped
        b._gate.sessions(0, time.monotonic())           # ...who leaves
        assert _pump(lambda: b._gate.dropped >= dropped + 20)


def test_the_idle_rate_lets_a_trickle_through():
    with _bridge(f"{_SOURCE} ! queue name=ingress_q ! fakesink name=cam_webrtcsink sync=false") as b:
        b._gate = IdleGate(idle_fps=10, grace_s=0, warmup_s=0)
        seen = _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        assert _pump(lambda: b._rx_frames >= 60)        # 1.2 s of camera at 50 fps
        assert 3 <= len(seen) <= 30                     # about ten a second, not fifty


@pytest.mark.parametrize("pipeline, env", [
    (f"{_SOURCE} ! queue ! fakesink name=cam_webrtcsink sync=false", {}),                        # no ingress_q
    (f"{_SOURCE} ! queue name=ingress_q ! fakesink sync=false", {}),                             # no sink
    (f"{_SOURCE} ! queue name=ingress_q ! fakesink name=cam_webrtcsink sync=false", {"CAM_WEBRTC_IDLE_FPS": "off"}),
])
def test_a_pipeline_the_gate_cannot_serve_runs_ungated(pipeline, env):
    with _bridge(pipeline, **env) as b:
        assert not b._gate.enabled
        b.pipeline.set_state(Gst.State.PLAYING)
        assert _pump(lambda: b._rx_frames >= 20)
        assert b._gate.dropped == 0


def test_the_heartbeat_names_the_idle_gate_and_the_shared_encoder(caplog):
    def status():
        return [r.getMessage() for r in caplog.records if r.getMessage().startswith("status:")][-1]

    with _bridge(f"{_SOURCE} ! queue name=ingress_q ! fakesink name=cam_webrtcsink sync=false",
                 CAM_WEBRTC_IDLE_FPS="2", CAM_WEBRTC_IDLE_GRACE_S="0") as b, \
            caplog.at_level(logging.INFO, logger="bridge_stream"):
        b._status_iv, b._rx_frames = 10, 5
        b._gate.warmup_s = 0
        b._gate.allow(time.monotonic() - 1)             # a first frame, a moment ago
        assert b._status_tick() is True
        assert " consumers=0 idle(2 fps)" in status()
        b._shared = object()
        b._shared_keyframes, b._keyframes.forced, b._keyframes.requests = 7, 3, 5
        b._consumers = 1
        b._gate.sessions(1, time.monotonic())
        b._status_tick()
        assert " consumers=1 shared-encoder(7 keyframes sent, 3 forced for 5 requests)" in status()
        assert "idle(" not in status()


# ---- the format seam: which debayer --------------------------------------------------------------------
_BAYER = ("videotestsrc is-live=true name=cam_src ! video/x-bayer,format=rggb,width=64,height=48,framerate=50/1 ! "
          "identity name=fmt_tap   videoscale name=fmt_scale add-borders=false ! capsfilter name=scale_caps ! "
          "videoconvert name=fmt_next ! video/x-raw,format=I420 ! fakesink name=cam_webrtcsink sync=false")
_HAS_BIN = Gst.ElementFactory.find("bayerbin") is not None      # the WebRTC image has it; the core's does not


def _seam(b):
    """Play `b` until frames arrive: (the element spliced in at the seam, the size the sink is given)."""
    seen = _count_buffers(b.pipeline)
    b.pipeline.set_state(Gst.State.PLAYING)
    assert _pump(lambda: len(seen) >= 5)
    st = b.pipeline.get_by_name("cam_webrtcsink").get_static_pad("sink").get_current_caps().get_structure(0)
    return (b.pipeline.get_by_name("fmt_adapt").get_factory().get_name(),
            (st.get_int("width")[1], st.get_int("height")[1]))


def test_a_bayer_preview_reduced_to_half_is_debayered_at_half_resolution_where_the_element_exists(caplog):
    caplog.set_level(logging.INFO)
    with _bridge(_BAYER, CAM_WEBRTC_MAX_SIZE="32") as b:
        factory, size = _seam(b)
        assert size == (32, 24)                                  # the same size either way
        assert factory == ("bayerbin" if _HAS_BIN else "bayer2rgb")
        # and the descriptor, worked out before any frame, says that size
        assert b._streamed_dims({"width": 64, "height": 48, "pixel_format": "bayer_rggb8"})["width"] == 32
    assert ("at half resolution" in caplog.text) == _HAS_BIN


@pytest.mark.parametrize("env, size", [
    ({"CAM_WEBRTC_MAX_SIZE": "32", "CAM_WEBRTC_DEBAYER": "full"}, (32, 24)),    # asked for the full debayer
    ({"CAM_WEBRTC_MAX_SIZE": "48"}, (48, 36)),                                    # well above half size
    ({}, (64, 48)),                                                               # nothing is reduced
])
def test_the_full_debayer_stays_wherever_binning_would_cost_resolution_or_was_declined(env, size):
    with _bridge(_BAYER, **env) as b:
        assert _seam(b) == ("bayer2rgb", size)


def test_the_descriptor_gives_the_size_the_viewer_receives():
    dims = lambda b, **d: (lambda out: (out["width"], out["height"]))(                    # noqa: E731
        b._streamed_dims({"width": 2448, "height": 2048, **d}))
    with _bridge(_BAYER, CAM_WEBRTC_MAX_SIZE="1280") as b:
        assert dims(b, pixel_format="mono8") == (1280, 1070)
        assert dims(b, pixel_format="bayer_rggb8") == ((1224, 1024) if _HAS_BIN else (1280, 1070))
    with _bridge(_BAYER, CAM_WEBRTC_MAX_SIZE="1280", CAM_WEBRTC_DEBAYER="full") as b:
        assert dims(b, pixel_format="bayer_rggb8") == (1280, 1070)
    with _bridge(_BAYER, CAM_WEBRTC_MAX_SIZE="1280", CAM_WEBRTC_DEBAYER="false") as b:    # the raw mosaic
        assert dims(b, pixel_format="bayer_rggb8") == (1280, 1070)
    with _bridge(_BAYER) as b:
        assert dims(b, pixel_format="bayer_rggb8") == (2448, 2048)
    # a static front-end (shm-raw) has bayer2rgb written into it: never binned
    with _bridge("videotestsrc name=cam_src ! videoscale name=fmt_scale ! capsfilter name=scale_caps ! "
                 "fakesink name=cam_webrtcsink", CAM_WEBRTC_MAX_SIZE="1280") as b:
        assert dims(b, pixel_format="bayer_rggb8") == (1280, 1070)


# ---- the shared encoder, on a pipeline -------------------------------------------------------------------
_SINK = "capsfilter name=first_caps_only ! fakesink name=cam_webrtcsink sync=false"


def _shared(profile=None, source=None, **env):
    """A Bridge on `source ! ingress_q ! <the shared x264 chain> ! sink`, as run.sh builds it."""
    chain = shared_encoder.chain("h264", "x264enc", 400_000)
    source = source or _SOURCE
    if profile:
        env["CAM_WEBRTC_PROFILE"] = profile
    return _bridge(f"{source} ! queue name=ingress_q ! {chain} ! {_SINK}",
                   CAM_WEBRTC_IDLE_FPS="off", CAM_WEBRTC_ENCODE="shared", **env)


def _caps_events(pipeline):
    """Every caps event that reaches the sink, which is made to behave as webrtcsink does: it takes the
    first caps it is sent and from then on accepts those and no others (a different second announcement
    is refused -- "Renegotiation is not supported" -- and the pipeline stops)."""
    seen, lock = [], pipeline.get_by_name("first_caps_only")

    def probe(_pad, info):
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            if not seen:
                lock.set_property("caps", event.parse_caps())
            seen.append(event.parse_caps().to_string())
        return Gst.PadProbeReturn.OK

    lock.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
    return seen


def _flows(b, frames, count):
    """`count` frames reach the sink (False as soon as the pipeline has failed instead)."""
    return _pump(lambda: len(frames) >= count or b._error) and not b._error


@pytest.mark.parametrize("preset, asked, stream", [
    # ultrafast uses no tool above constrained-baseline, so that is what the stream is, whatever is asked
    ("ultrafast", None, "constrained-baseline"), ("ultrafast", "baseline", "constrained-baseline"),
    ("ultrafast", "main", "constrained-baseline"), ("ultrafast", "high", "constrained-baseline"),
    # a preset that uses CABAC and the 8x8 transform produces what is asked
    ("superfast", None, "constrained-baseline"), ("superfast", "baseline", "constrained-baseline"),
    ("superfast", "main", "main"), ("superfast", "high", "high"),
])
def test_the_shared_chain_announces_complete_caps_exactly_once(preset, asked, stream, caplog):
    """h264parse announces caps when the encoder's arrive and again once it has parsed the SPS. webrtcsink
    refuses a second announcement that differs and the bridge dies not-negotiated, so there must be
    exactly one, and it has to say what the stream is: the sink here accepts its first caps and no others."""
    with _shared(asked, CAM_WEBRTC_X264_PRESET=preset) as b, caplog.at_level(logging.WARNING, logger="bridge_stream"):
        assert b._shared_name == "x264enc" and b._shared_codec == "h264"
        enc = b.pipeline.get_by_name("shared_enc")
        # x264enc takes no properties once started: they are in place before the pipeline runs
        assert enc.get_property("bframes") == 0 and b._shared_preset() == preset
        assert "zerolatency" in enc.get_property("tune").value_nicks
        caps, frames = _caps_events(b.pipeline), _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        assert _flows(b, frames, 15), "the sink refused a second announcement"       # well past the SPS
        assert len(caps) == 1, caps
        st = Gst.Caps.from_string(caps[0]).get_structure(0)
        assert st.get_name() == "video/x-h264" and st.get_string("stream-format") == "byte-stream"
        assert st.get_string("profile") == stream
        assert (st.get_int("width")[1], st.get_int("height")[1]) == (W, H) and st.get_string("level")
        assert st.get_string("chroma-format") == "4:2:0" and st.get_string("coded-picture-structure") == "frame"
        pinned = b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert f"profile=(string){stream}" in pinned    # the encoder is asked for what it will produce
        said = [r.getMessage() for r in caplog.records if "ultrafast preset produces" in r.getMessage()]
        assert len(said) == (1 if preset == "ultrafast" and asked in ("main", "high") else 0), said
        assert b._shared_keyframes >= 1                 # the stream opened with one


def _encoded(width, height, fps, count=30):
    """`count` frames of real H.264 at this geometry as (bytes, pts), and the caps x264enc gave them."""
    pipeline = Gst.parse_launch(
        f"videotestsrc num-buffers={count} ! video/x-raw,format=I420,width={width},height={height},framerate={fps}/1 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bitrate=600 key-int-max=10 ! "
        "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline ! appsink name=out sync=false")
    out, frames, caps = pipeline.get_by_name("out"), [], None
    pipeline.set_state(Gst.State.PLAYING)
    try:
        while len(frames) < count:
            sample = out.emit("try-pull-sample", 10 * Gst.SECOND)
            assert sample is not None
            caps = sample.get_caps()
            buf = sample.get_buffer()
            frames.append((buf.extract_dup(0, buf.get_size()), buf.pts))
    finally:
        pipeline.set_state(Gst.State.NULL)
    return frames, caps


@pytest.mark.parametrize("announced", ["3", "3.0"])
def test_a_hardware_encoder_that_spells_level_3_as_3_0_negotiates_and_is_announced_once(announced):
    """nvv4l2h264enc, asked for level 3, encodes level 3 and announces "3.0". Pinned to "3" alone the
    filter refused it and every SD-sized preview died not-negotiated; let through as it is, the parser
    announces "3.0" and then, having read the SPS, "3" -- which webrtcsink refuses. Here a real level 3
    stream arrives labelled the way that encoder labels it, through the chain built for that encoder."""
    frames, encoder_caps = _encoded(640, 480, 25)
    labelled = encoder_caps.copy()
    assert labelled.get_structure(0).get_string("level") == "3"
    labelled.set_value("level", announced)
    chain = shared_encoder.chain("h264", "nvv4l2h264enc", 600_000)
    tail = chain[chain.index("capsfilter name=shared_enc_caps"):]         # everything behind the encoder itself
    with _bridge(CAM_WEBRTC_ENCODE="shared") as b:
        b.pipeline = Gst.parse_launch(f"appsrc name=encoder format=time ! {tail} ! {_SINK}")
        bus, errors = b.pipeline.get_bus(), []
        bus.add_signal_watch()
        bus.connect("message::error", lambda _bus, msg: errors.append(msg.parse_error()[0].message))
        src = b.pipeline.get_by_name("encoder")
        src.set_property("caps", labelled)
        b._shared, b._shared_name, b._shared_codec = Gst.ElementFactory.make("x264enc"), "nvv4l2h264enc", "h264"

        class _Info:                                    # the raw caps that reach the encoder's input
            def get_event(self):
                return Gst.Event.new_caps(Gst.Caps.from_string("video/x-raw,format=NV12,width=640,height=480,framerate=25/1"))

        assert b._on_shared_caps(None, _Info()) == Gst.PadProbeReturn.OK
        assert "level=(string){ 3, 3.0 }" in b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        seen, arrived = _caps_events(b.pipeline), _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        for data, pts in frames:
            buf = Gst.Buffer.new_wrapped(data)
            buf.pts = pts
            assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        assert _pump(lambda: len(arrived) >= 20 or errors), "nothing reached the sink"
        assert errors == []
        assert len(seen) == 1, seen
        assert Gst.Caps.from_string(seen[0]).get_structure(0).get_string("level") == "3"


def _hardware_like(width, height, fps, **env):
    """The chain the hardware H.264 encoder gets -- filter, level respelling, parser -- around x264enc, which
    this suite has, for the launcher code that only looks at the caps it sets."""
    chain = shared_encoder.chain("h264", "nvv4l2h264enc", 600_000)
    head = "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc name=shared_enc bitrate=600000"
    assert head in chain
    stand_in = "x264enc name=shared_enc bitrate=600 tune=zerolatency speed-preset=ultrafast"
    source = (f"videotestsrc name=cam_src is-live=true ! video/x-raw,format=I420,width={width},height={height},"
              f"framerate={fps}/1")
    return _bridge(f"{source} ! queue name=ingress_q ! {chain.replace(head, stand_in)} ! {_SINK}",
                   CAM_WEBRTC_IDLE_FPS="off", CAM_WEBRTC_ENCODE="shared", **env)


def test_keyframes_the_sink_asks_for_are_served_at_most_once_a_second():
    with _shared(CAM_WEBRTC_KEYFRAME_S="0") as b:       # no keyframes on the clock: only what is asked for
        frames = _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        assert _flows(b, frames, 10)
        before = b._shared_keyframes
        sinkpad = b.pipeline.get_by_name("cam_webrtcsink").get_static_pad("sink")
        for _ in range(12):                             # what one joining viewer sends: a burst
            sinkpad.push_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))
        assert b._keyframes.requests == 12
        assert _pump(lambda: b._shared_keyframes > before)          # the encoder was asked, and answered
        assert b._keyframes.forced == 1
        first = time.monotonic()
        sinkpad.push_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))
        assert b._keyframes.forced == 1                 # too soon: deferred, not dropped
        assert _pump(lambda: b._keyframes.forced == 2)
        assert time.monotonic() - first > 0.5
        assert _pump(lambda: b._shared_keyframes >= before + 2)
        assert b._shared_keyframes <= before + 3        # a burst of thirteen requests, not thirteen keyframes


def test_keyframes_come_on_the_clock_until_the_sink_is_ready_and_while_somebody_watches():
    with _shared() as b:
        asked = lambda: b._keyframes.requests       # noqa: E731
        assert b._periodic_keyframe() is True and asked() == 1      # the sink's discovery needs one
        b._sink_ready = True
        assert b._periodic_keyframe() is True and asked() == 1      # registered, nobody watching: nothing
        b._consumers = 1
        assert b._periodic_keyframe() is True and asked() == 2
        b._stopping = True
        assert b._periodic_keyframe() is False and b._force_keyframe() is False


def test_the_hardware_encoders_take_level_and_gop_from_the_stream():
    """nvv4l2h264enc leaves profile and level out of its caps and takes its GOP as a property: both are
    set from the caps the stream really has, when they arrive (a stand-in element carries the name)."""
    with _hardware_like(64, 48, 25, CAM_WEBRTC_PROFILE="high") as b:
        b._shared_name = "nvv4l2h264enc"
        level = lambda: b.pipeline.get_by_name("shared_enc_level").get_property("caps").to_string()    # noqa: E731

        class _Info:
            def __init__(self, caps): self._event = Gst.Event.new_caps(Gst.Caps.from_string(caps))
            def get_event(self): return self._event

        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1")) \
            == Gst.PadProbeReturn.OK
        pinned = b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert "profile=(string)high" in pinned and "level=(string){ 4, 4.0 }" in pinned
        assert level() == "video/x-h264, level=(string)4"
        assert b._shared.get_property("key-int-max") == 60          # CAM_WEBRTC_KEYFRAME_S (2 s) at 30 fps
        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=2448,height=2048,framerate=24/1")) \
            == Gst.PadProbeReturn.OK
        assert "level=(string){ 5, 5.0 }" in b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=1280,height=720,framerate=30/1")) \
            == Gst.PadProbeReturn.OK
        assert "level=(string)3.1" in b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert level() == "video/x-h264, level=(string)3.1"
        # the level pinned is the one the encoder will WRITE: its bitrate can need more than the geometry,
        # and it writes nothing below 2.1
        b._shared_bitrate = lambda: 20_000_000
        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=640,height=480,framerate=30/1")) \
            == Gst.PadProbeReturn.OK
        assert "level=(string)3.2" in b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        b._shared_bitrate = lambda: 500_000
        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=320,height=240,framerate=30/1")) \
            == Gst.PadProbeReturn.OK
        assert level() == "video/x-h264, level=(string)2.1"
        # caps without a geometry are left alone
        before = b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12")) == Gst.PadProbeReturn.OK
        assert b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string() == before


def test_the_shared_encoders_bitrate_and_preset_are_read_from_the_element():
    with _shared(CAM_WEBRTC_KEYFRAME_S="soon") as b:    # an interval that is no number: the default, not a crash
        assert b._shared_bitrate() == 400              # x264enc counts in kbit/s; the hardware encoders in bit/s
        assert b._shared_preset() == "ultrafast"
        b._shared = object()
        assert b._shared_bitrate() == 0 and b._shared_preset() is None


def test_only_caps_and_keyframe_requests_concern_the_shared_encoders_probes():
    with _shared(CAM_WEBRTC_KEYFRAME_S="0") as b:
        class _Info:
            def __init__(self, event): self._event = event
            def get_event(self): return self._event

        ok = Gst.PadProbeReturn.OK
        before = b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert b._on_shared_caps(None, _Info(Gst.Event.new_eos())) == ok
        assert b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string() == before
        other = Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, Gst.Structure.new_empty("something-else"))
        assert b._on_shared_upstream(None, _Info(Gst.Event.new_reconfigure())) == ok
        assert b._on_shared_upstream(None, _Info(other)) == ok
        assert b._keyframes.requests == 0
        key = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        assert b._on_shared_upstream(None, _Info(key)) == Gst.PadProbeReturn.DROP and b._keyframes.requests == 1


def test_the_hardware_encoders_are_asked_for_a_keyframe_through_their_own_action():
    """nvv4l2h26Xenc ignores the force-key-unit event; it has a `force-IDR` action instead."""
    class _Encoder:
        def __init__(self, fails=False): self.emitted, self.fails = [], fails
        def emit(self, signal):
            if self.fails:
                raise TypeError("no such signal")
            self.emitted.append(signal)

    with _bridge(CAM_WEBRTC_ENCODE="shared") as b:
        for name in ("nvv4l2h264enc", "nvv4l2h265enc"):
            b._shared, b._shared_name = _Encoder(), name
            assert b._force_keyframe() is False and b._shared.emitted == ["force-IDR"]      # False: a one-shot timer
        assert b._keyframes.forced == 2
        b._shared = _Encoder(fails=True)                # an element without it: the periodic keyframe still comes
        assert b._force_keyframe() is False and b._keyframes.forced == 3


def test_a_level_the_clamp_cannot_cover_is_said_and_one_that_cannot_be_computed_is_left_to_the_encoder(caplog):
    with _hardware_like(64, 48, 25, CAM_WEBRTC_MAX_LEVEL="3.1") as b, caplog.at_level(logging.WARNING, logger="bridge_stream"):
        b._shared_name = "nvv4l2h264enc"

        class _Info:
            def __init__(self, caps): self._event = Gst.Event.new_caps(Gst.Caps.from_string(caps))
            def get_event(self): return self._event

        assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1")) \
            == Gst.PadProbeReturn.OK
        assert any("needs a level above the clamp 3.1" in r.getMessage() for r in caplog.records)
        with patch.object(bridge_stream, "h264_level_for", side_effect=ValueError("no such geometry")):
            assert b._on_shared_caps(None, _Info("video/x-raw,format=NV12,width=640,height=480,framerate=30/1")) \
                == Gst.PadProbeReturn.OK
        assert any("h264 level math failed (no such geometry)" in r.getMessage() for r in caplog.records)
        pinned = b.pipeline.get_by_name("shared_enc_caps").get_property("caps").to_string()
        assert "level" not in pinned                    # nothing pinned: the encoder's own
        assert b.pipeline.get_by_name("shared_enc_level").get_property("caps").to_string() == "video/x-h264"


def test_in_shared_mode_the_sinks_own_encoder_hooks_stand_aside():
    with _shared() as b:
        caps = Gst.Caps.from_string("video/x-h264")
        assert b._on_request_encoded_filter(None, "consumer", "video_0", caps) is None


def test_capture_to_encoder_latency_is_taken_at_the_shared_encoders_input():
    """What reaches the sink in shared mode is encoded and carries byte offsets, not the capture stamp."""
    source = (f'appsrc name=cam_src is-live=true format=time caps="video/x-raw,format=I420,width={W},'
              f'height={H},framerate=25/1"')
    with _shared(source=source) as b:
        frames = _count_buffers(b.pipeline)
        b.pipeline.set_state(Gst.State.PLAYING)
        src = b.pipeline.get_by_name("cam_src")
        for i in range(20):
            buf = Gst.Buffer.new_wrapped(bytes(W * H * 3 // 2))
            buf.pts, buf.duration = i * Gst.SECOND // 25, Gst.SECOND // 25
            buf.offset_end = time.time_ns() - 30_000_000            # captured 30 ms ago
            assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        # every frame is measured where it enters the encoder's (leaky) queue, however many it then encodes
        assert _pump(lambda: len(b._lat_enc) == 20 and len(frames) >= 1)
        assert all(25 <= lat <= 5000 for lat in b._lat_enc)
