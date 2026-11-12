#!/usr/bin/env python3
"""Unit tests for session_reaper (pure; no gi/GStreamer needed). Run: python3 test_session_reaper.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_reaper import (STALE_S_DEFAULT, SessionReaper, flatten_stats, parse_max_sessions,  # noqa: E402
                            parse_stale_s, receiver_key)


class _St:
    """Just enough GstStructure: ordered fields, nested structures, and a field that will not marshal."""

    def __init__(self, **fields):
        self._f = {k.replace("_", "-"): v for k, v in fields.items()}

    def n_fields(self):
        return len(self._f)

    def nth_field_name(self, i):
        return list(self._f)[i]

    def get_value(self, name):
        if isinstance(self._f[name], Exception):
            raise self._f[name]
        return self._f[name]


def _session(sent, twcc_recv, rb_lsr, rtt=0.002):
    """The shape webrtcsink reports for one session (the fields that matter, plus noise)."""
    return _St(
        ice_candidate_pair_webrtcnicetransport0=_St(
            timestamp=1.0, id="pair", gst_twcc_stats=_St(packets_recv=twcc_recv, bitrate_recv=twcc_recv * 9000,
                                                         packet_loss_pct=0.0)),
        rtp_outbound_stream_stats_1=_St(timestamp=2.0, bytes_sent=sent * 1200, packets_sent=sent,
                                        gst_rtpsource_stats=_St(sr_ntptime=sent * 7, packets_sent=sent)),
        rtp_remote_inbound_stream_stats_1=_St(timestamp=3.0, round_trip_time=rtt,
                                              gst_rtpsource_stats=_St(rb_lsr=rb_lsr, rb_dlsr=rb_lsr + 1,
                                                                      rb_exthighestseq=rb_lsr * 3)),
        consumer_stats=_St(video_encoders=TypeError("unknown type GstValueArray")))


def test_flatten_skips_timestamps_and_fields_that_do_not_marshal():
    flat = flatten_stats(_session(10, 5, 100))
    assert flat["ice-candidate-pair-webrtcnicetransport0/gst-twcc-stats/packets-recv"] == "5"
    assert flat["rtp-outbound-stream-stats-1/gst-rtpsource-stats/packets-sent"] == "10"
    assert not any(k.endswith("timestamp") for k in flat)
    assert not any("video-encoders" in k for k in flat)


def test_receiver_key_ignores_everything_the_sender_does_alone():
    alive = receiver_key(flatten_stats(_session(10, 5, 100)))
    # a dead link: the sender keeps sending, sender reports keep going out, nothing comes back
    assert receiver_key(flatten_stats(_session(900, 5, 100))) == alive
    # the receiver talking, either way, moves it
    assert receiver_key(flatten_stats(_session(10, 6, 100))) != alive       # transport-cc feedback
    assert receiver_key(flatten_stats(_session(10, 5, 101))) != alive       # a receiver report
    assert receiver_key(flatten_stats(_session(10, 5, 100, rtt=0.004))) != alive
    assert receiver_key({}) == ()


def test_a_session_whose_receiver_goes_quiet_is_ended_once():
    r = SessionReaper(stale_s=10)
    assert r.observe(0, {"a": 1, "b": 1}) == []
    assert r.observe(4, {"a": 2, "b": 2}) == []
    assert r.observe(8, {"a": 3, "b": 2}) == []                 # b last spoke at t=4
    assert r.quiet_s("b", 12) == 8 and r.quiet_s("a", 12) == 4 and r.quiet_s("zz", 12) == 0
    assert r.observe(13.9, {"a": 4, "b": 2}) == []
    out = r.observe(14, {"a": 5, "b": 2})
    assert [sid for sid, _ in out] == ["b"] and "10 s" in out[0][1]
    assert r.observe(16, {"a": 6, "b": 2}) == []                # asked for; the sink removes it asynchronously
    assert r.observe(18, {"a": 7}) == []                        # gone and forgotten
    assert r.observe(20, {"a": 8, "b": 9}) == []                # the same id again is a new session


def test_an_ended_session_that_lingers_is_asked_for_again():
    r = SessionReaper(stale_s=5)
    r.observe(0, {"a": 1})
    r.observe(1, {"a": 2})
    assert [s for s, _ in r.observe(6, {"a": 2})] == ["a"]
    assert r.observe(10, {"a": 2}) == []
    assert [s for s, _ in r.observe(16, {"a": 2})] == ["a"]


def _seen_feedback(stale_s, **kw):
    """A reaper that has watched one viewer talk and leave: the statistics of this build are readable."""
    r = SessionReaper(stale_s=stale_s, **kw)
    r.observe(-10, {"earlier": 1})
    r.observe(-9, {"earlier": 2})
    r.observe(-8, {})
    assert r.feedback_seen
    return r


def test_a_session_that_never_gets_as_far_as_talking_ages_from_when_it_appeared():
    r = _seen_feedback(5)
    assert r.observe(100, {"a": None}) == []                    # no statistics yet
    assert r.observe(103, {"a": ()}) == []                      # statistics, no receiver in them: a change
    assert r.observe(107.9, {"a": ()}) == []
    assert [s for s, _ in r.observe(108, {"a": ()})] == ["a"]


def test_nothing_is_ended_for_silence_until_receiver_feedback_has_been_seen_at_all():
    """The policy reads webrtcsink's statistics by field name. Where those names differ, or the values
    will not marshal, every key stands still: that must not end every live viewer every stale_s."""
    r = SessionReaper(stale_s=5)
    for t in range(0, 600, 2):                                  # two viewers, ten minutes, keys that never move
        assert r.observe(t, {"a": (), "b": (("x", "1"),)}) == []
    assert not r.feedback_seen and r.blind(600)                 # ...and the caller is told why
    assert not SessionReaper(stale_s=5).blind(600)              # nobody watched yet: nothing to say
    r2 = SessionReaper(stale_s=5)
    r2.observe(0, {"a": None})
    assert not r2.blind(4.9) and r2.blind(5)
    # statistics APPEARING is not the far end talking
    r2.observe(6, {"a": (("rb", "0"),)})
    assert not r2.feedback_seen
    # a key that moves is: from then on the silent ones go, and the check is no longer blind
    out = r.observe(600, {"a": (), "b": (("x", "2"),)})
    assert r.feedback_seen and not r.blind(600)
    assert [s for s, _ in out] == ["a"]                         # ten minutes of nothing, and now it counts
    assert r.observe(602, {"b": (("x", "2"),)}) == []
    assert [s for s, _ in r.observe(605, {"b": (("x", "2"),)})] == ["b"]
    # the cap needs no evidence
    capped = SessionReaper(stale_s=5, max_sessions=1)
    capped.observe(0, {"a": ()})
    assert [s for s, _ in capped.observe(1, {"a": (), "b": ()})] == ["a"]
    # and with the liveness policy off there is nothing to be blind about
    assert not SessionReaper(stale_s=0, max_sessions=1).blind(1e9)


def test_silence_only_counts_while_media_is_going_out():
    # a camera that stalls leaves a LIVE receiver with nothing to acknowledge
    r = SessionReaper(stale_s=10)
    r.observe(0, {"a": 1})
    r.observe(2, {"a": 2})
    assert r.observe(4, {"a": 2}) == []                         # 2 s quiet, frames still going out
    for t in range(6, 120, 2):                                  # the camera is gone for two minutes
        assert r.observe(t, {"a": 2}, sending=False) == []
    assert r.quiet_s("a", 118) == 2
    assert r.observe(120, {"a": 2}) == []                       # frames again: the count resumes at 2 s
    assert r.observe(126, {"a": 3}) == []                       # ...and the viewer answers
    # a viewer that really is gone is still ended once frames flow; the stall is not counted either way
    r = _seen_feedback(10)
    r.observe(0, {"a": 1})
    r.observe(4, {"a": 1})
    for t in (6, 8, 10, 12, 14):
        assert r.observe(t, {"a": 1}, sending=False) == []
    assert r.observe(18, {"a": 1}) == []                        # 4 s before the stall + 4 s after it
    assert [s for s, _ in r.observe(20, {"a": 1})] == ["a"]
    # the cap does not wait for frames
    r = SessionReaper(stale_s=10, max_sessions=1)
    r.observe(0, {"a": 1}, sending=False)
    assert [s for s, _ in r.observe(2, {"a": 1, "b": 1}, sending=False)] == ["a"]


def test_cap_ends_the_oldest_and_counts_what_is_already_going():
    r = SessionReaper(stale_s=0, max_sessions=2)
    assert r.observe(0, {"a": 1}) == [] and r.observe(1, {"a": 2, "b": 1}) == []
    out = r.observe(2, {"a": 3, "b": 2, "c": 1})
    assert [s for s, _ in out] == ["a"] and "more than 2" in out[0][1]
    assert r.observe(3, {"a": 4, "b": 3, "c": 2}) == []         # a is on its way out: b and c fit
    assert [s for s, _ in r.observe(4, {"a": 5, "b": 4, "c": 3, "d": 1})] == ["b"]
    assert r.observe(5, {"c": 4, "d": 2}) == []


def test_both_policies_together_do_not_end_more_than_they_must():
    # `old` is stale AND over the cap: it goes once, for the first reason, and `new` -- now alone -- stays
    r = _seen_feedback(10, max_sessions=1)
    r.observe(0, {"old": 1})
    out = r.observe(10, {"old": 1, "new": 1})
    assert [s for s, _ in out] == ["old"] and "no receiver feedback" in out[0][1]
    assert r.observe(12, {"old": 1, "new": 2}) == []


def test_disabled_reaper_ends_nothing():
    r = SessionReaper(stale_s=0, max_sessions=0)
    assert not r.enabled
    assert all(r.observe(t, {"a": 1, "b": 1, "c": 1}) == [] for t in (0, 100, 10000))
    assert SessionReaper().enabled and SessionReaper(stale_s=0, max_sessions=1).enabled


def test_settings():
    assert parse_stale_s(None) == parse_stale_s("") == parse_stale_s("x") == STALE_S_DEFAULT
    assert [parse_stale_s(v) for v in ("0", "off", "false", "-3", "7", "2.5")] == [0, 0, 0, 0, 7, 2.5]
    assert [parse_max_sessions(v) for v in (None, "", "0", "-1", "two", "2", " 3 ")] == [0, 0, 0, 0, 0, 2, 3]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
