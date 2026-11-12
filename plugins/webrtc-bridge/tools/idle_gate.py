"""Stop preparing video nobody is watching (pure; no gi/GStreamer).

With no viewer webrtcsink encodes nothing, but everything in front of it kept running: the debayer, the
scale, the 16-bit stretch and the convert to I420 on every frame. On a 5 MP Bayer camera at 24 fps that
is 80% of an Orin core for an empty room (measured; a viewer adds only 32% on top). The gate sits at the
head of that chain and, while no session exists, lets through only a trickle.

A trickle rather than nothing, because the chain is worth keeping warm: caps stay negotiated, the
discovery descriptor's geometry stays true and the heartbeat keeps measuring latency. The cost is the
idle rate over the camera rate -- 1 frame in 24 by default.

Three things keep the gate from getting in the way:
  start-up  nothing is held back until the sink is ready for viewers. webrtcsink discovers its codecs by
            running real frames through real encoders (ten seconds with every software codec an image
            carries) and registers with the signalling server only afterwards; at 1 fps that would take
            minutes. The bridge reports the moment (ready(), when the signaller has its id from the
            server); where it cannot, the gate falls back to a fixed warm-up after the first frame.
  a viewer  opens the gate the moment its session is created, before its first frame is needed.
  grace     the gate closes only `grace_s` after the last viewer left (or after the sink became ready),
            so a reconnect -- the dashboard cycling a session -- never sees the chain throttled.

bridge_stream owns one IdleGate: a pad probe asks allow() per frame, the session signals call sessions().
"""

IDLE_FPS_DEFAULT = 1.0
GRACE_S_DEFAULT = 5.0
WARMUP_S = 10.0


def parse_idle_fps(value):
    """CAM_WEBRTC_IDLE_FPS -> (gate enabled, frames per second while idle).
    unset -> (True, 1.0); a number >= 0 -> that rate (0 = nothing at all while idle);
    off / false / no / full -> (False, _): the gate is disabled and every frame is processed."""
    v = str(value if value is not None else "").strip().lower()
    if v == "":
        return True, IDLE_FPS_DEFAULT
    if v in ("off", "false", "no", "full", "none"):
        return False, 0.0
    try:
        fps = float(v)
    except ValueError:
        return True, IDLE_FPS_DEFAULT
    return (True, fps) if fps >= 0 else (False, 0.0)


def parse_grace_s(value):
    try:
        s = float(str(value if value is not None else "").strip())
    except ValueError:
        return GRACE_S_DEFAULT
    return s if s >= 0 else GRACE_S_DEFAULT


class IdleGate:
    def __init__(self, idle_fps=IDLE_FPS_DEFAULT, grace_s=GRACE_S_DEFAULT, warmup_s=WARMUP_S, enabled=True):
        self.enabled = bool(enabled)
        self.idle_fps = float(idle_fps)
        self.grace_s = float(grace_s)
        self.warmup_s = float(warmup_s)
        self._sessions = 0
        self._started = None       # time of the first frame
        self._await_ready = False  # the owner will call ready(); until then nothing is held back
        self._ready_at = None      # when the sink became ready for viewers
        self._idle_since = None    # when the session count last fell to zero (None = never had one)
        self._last_passed = None   # time of the last frame let through while idle
        self.passed = 0
        self.dropped = 0

    def sessions(self, count, now):
        """The number of viewer sessions, whenever it is learned (signals, or a resync)."""
        count = max(0, int(count))
        if count == 0 and self._sessions > 0:
            self._idle_since = now
        self._sessions = count

    def await_ready(self):
        """The owner can tell when the sink is ready for viewers and will call ready(): hold nothing
        back before that, however long it takes, instead of guessing with the fixed warm-up."""
        self._await_ready = True

    def ready(self, now):
        """The sink finished its start-up (codec discovery) and is taking viewers."""
        if self._ready_at is None:
            self._ready_at = now

    def idle(self, now):
        """True while frames are being held back to the trickle."""
        if not self.enabled or self._sessions > 0 or self._started is None:
            return False
        if self._await_ready:
            if self._ready_at is None:
                return False
            since = self._ready_at if self._idle_since is None else max(self._ready_at, self._idle_since)
            return now - since >= self.grace_s
        if now - self._started < self.warmup_s:
            return False
        return self._idle_since is None or now - self._idle_since >= self.grace_s

    def allow(self, now):
        """Per frame, on the streaming thread: may this frame go on down the chain?"""
        if self._started is None:
            self._started = now
        if not self.idle(now):
            self._last_passed = None
            self.passed += 1
            return True
        if self.idle_fps > 0 and (self._last_passed is None or now - self._last_passed >= 1.0 / self.idle_fps):
            self._last_passed = now
            self.passed += 1
            return True
        self.dropped += 1
        return False
