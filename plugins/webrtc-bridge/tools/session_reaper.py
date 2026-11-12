"""Which viewer sessions to end (pure; no gi/GStreamer).

webrtcsink removes a session only when its connection or ICE state becomes Failed, or when signalling
reports it ended. On the bridge images built on Ubuntu 24.04 (GStreamer 1.24 + libnice 0.1.21) ICE has
consent freshness off, so an established connection whose far end vanishes -- a radio dropout, a laptop
lid -- never fails. The session then lives until the kernel gives up on the signalling socket: measured
15 min 40 s on an Orin, with the session's encoder running and its stream going out over the radio the
whole time, and every reconnect of the viewer adding another. (A 26.04-based bridge has consent
freshness on and drops such a session by itself after ~30 s; this is then only quicker.)

Two policies, both driven from webrtcsink's own per-session statistics:

  liveness  end a session whose RECEIVER has said nothing for `stale_s`. What counts as the receiver
            talking was established on a running bridge by cutting a viewer's link and watching which
            statistics froze: the transport-wide congestion-control feedback on the candidate pair
            (`gst-twcc-stats`; changes at every sample while media flows) and the receiver-report block
            of the remote source (`rb-*`, round-trip time; about every 5 s). Sender-side counters
            keep climbing into a dead link and are not part of it.
            Silence only counts while the bridge is SENDING. A camera that stalls -- a reconnect, a
            paused replay -- leaves a live receiver with nothing to acknowledge: its feedback stopped
            8 s after the last frame, and it must not lose its session for that.
            It also only counts once this process has SEEN receiver feedback, from any session. The
            policy reads another element's statistics by field name; on a build where those names
            differ, or will not marshal, every key would stand still and every live viewer would be
            ended stale_s after it connected, again and again. Without that evidence nothing is ended
            (blind() says so) -- failing to reap costs CPU, the opposite costs the operators' video.
  cap       never more than `max_sessions`: a newcomer beyond it ends the oldest. Off by default; the
            right number is how many operators watch one camera.

bridge_stream feeds observe() from a timer and acts on what it returns. Kept free of GStreamer so the
policy is unit-testable (test_session_reaper.py), like encoder_policy.
"""

STALE_S_DEFAULT = 20.0     # four receiver-report intervals; consent freshness itself allows 30 s
_REISSUE_S = 10.0          # an ended session still listed after this long is asked for again


def parse_stale_s(value):
    """CAM_WEBRTC_STALE_S: seconds, 0 / off = the liveness policy is disabled. Unset -> the default."""
    v = str(value if value is not None else "").strip().lower()
    if v == "":
        return STALE_S_DEFAULT
    if v in ("off", "false", "no", "none"):
        return 0.0
    try:
        s = float(v)
    except ValueError:
        return STALE_S_DEFAULT
    return s if s > 0 else 0.0


def parse_max_sessions(value):
    """CAM_WEBRTC_MAX_SESSIONS: a positive integer, anything else = no cap."""
    try:
        n = int(str(value if value is not None else "").strip())
    except ValueError:
        return 0
    return n if n > 0 else 0


def flatten_stats(st, path="", out=None):
    """Every scalar of a nested stats structure as {path: str(value)}, the per-sample `timestamp`
    fields left out (they change by themselves). Duck-typed over GstStructure: n_fields(),
    nth_field_name(i), get_value(name) -- and a field PyGObject cannot marshal is skipped."""
    out = {} if out is None else out
    for i in range(st.n_fields()):
        name = st.nth_field_name(i)
        if name == "timestamp":
            continue
        try:
            value = st.get_value(name)
        except Exception:   # noqa: BLE001 -- e.g. GstValueArray has no PyGObject conversion
            continue
        if hasattr(value, "n_fields") and hasattr(value, "nth_field_name"):
            flatten_stats(value, f"{path}/{name}" if path else name, out)
        else:
            out[f"{path}/{name}" if path else name] = str(value)
    return out


def receiver_key(flat):
    """The part of a session's flattened statistics that only moves while the receiver is talking."""
    return tuple(sorted((k, v) for k, v in flat.items()
                        if "gst-twcc-stats/" in k or "/rb-" in k or k.endswith("/round-trip-time")))


class SessionReaper:
    def __init__(self, stale_s=STALE_S_DEFAULT, max_sessions=0):
        self.stale_s = float(stale_s or 0)
        self.max_sessions = int(max_sessions or 0)
        self._seen = {}        # session id -> [first seen, receiver key, when the key last changed]
        self._ended = {}       # session id -> when it was last returned for ending
        self._last_look = None
        self.feedback_seen = False   # some session's receiver key has been seen to move

    @property
    def enabled(self):
        return self.stale_s > 0 or self.max_sessions > 0

    def quiet_s(self, sid, now):
        """How long the receiver of `sid` has said nothing (0 for a session never observed)."""
        entry = self._seen.get(sid)
        return now - entry[2] if entry else 0.0

    def blind(self, now):
        """True when the liveness policy has nothing to go on: a session has been watched for stale_s
        and no session of this process has ever shown receiver feedback. Then nothing is ended for
        silence, and the caller should say so once."""
        if self.stale_s <= 0 or self.feedback_seen:
            return False
        return any(now - entry[2] >= self.stale_s for entry in self._seen.values())

    def observe(self, now, keys, sending=True):
        """One look at the sink's sessions: {session id: receiver key, or None when the sink has no
        statistics for it yet}. Returns [(session id, reason)] to end, oldest first.

        `sending`: did media go out since the previous look? When it did not, the time since then is
        not counted as anybody's silence (the cap still applies).

        A session with no receiver feedback at all ages from when it was first seen, so one that never
        gets as far as talking is ended like one that stopped -- once feedback has been seen from some
        session at all (see the module docstring). A session already returned is not returned again
        until _REISSUE_S later (the sink removes it asynchronously)."""
        since_last = now - self._last_look if self._last_look is not None else 0.0
        self._last_look = now
        for sid in [s for s in self._seen if s not in keys]:
            del self._seen[sid]
            self._ended.pop(sid, None)
        for sid, key in keys.items():
            entry = self._seen.get(sid)
            if entry is None:
                self._seen[sid] = [now, key, now]
            elif key != entry[1]:
                # feedback = a key that MOVED between two looks that both had statistics, and has
                # receiver fields in it; statistics merely appearing prove nothing about the far end
                if key and entry[1] is not None:
                    self.feedback_seen = True
                entry[1], entry[2] = key, now
            elif not sending:
                entry[2] = min(now, entry[2] + since_last)
        pending = {s for s, at in self._ended.items() if now - at < _REISSUE_S}
        to_end = []
        oldest_first = sorted(self._seen, key=lambda s: self._seen[s][0])
        if self.stale_s > 0 and self.feedback_seen:
            for sid in oldest_first:
                quiet = now - self._seen[sid][2]
                if quiet >= self.stale_s and sid not in pending:
                    to_end.append((sid, f"no receiver feedback for {quiet:.0f} s"))
        if self.max_sessions > 0:
            going = pending | {sid for sid, _ in to_end}
            staying = [s for s in oldest_first if s not in going]
            for sid in staying[:max(0, len(staying) - self.max_sessions)]:
                to_end.append((sid, f"more than {self.max_sessions} session(s): the oldest gives way"))
        for sid, _ in to_end:
            self._ended[sid] = now
        return to_end
