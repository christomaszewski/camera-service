"""Per-encoder property policy for a LIVE, loss-tolerant WebRTC stream (pure; no gi/GStreamer).

bridge_stream applies the result inside webrtcsink's `encoder-setup` signal, setting only the
properties the encoder element actually has. Kept Gst-free so the policy is unit-testable
(test_encoder_policy.py) on a runner without the media stack, like h264_level."""

_KEYFRAME_S_DEFAULT = 2.0
_X264_PRESET_DEFAULT = "superfast"


def keyframe_interval_frames(fps, seconds):
    """GOP length in FRAMES for a keyframe every `seconds` at `fps`. Bounded below at 1; an unknown /
    nonsensical fps assumes 10 (a conservative preview rate -- a SHORT gop is the safe error)."""
    try:
        fps = float(fps or 0)
        seconds = float(seconds)
    except (TypeError, ValueError):
        fps, seconds = 0.0, _KEYFRAME_S_DEFAULT
    if not fps > 0:
        fps = 10.0
    if not seconds > 0:
        seconds = _KEYFRAME_S_DEFAULT
    return max(1, int(round(fps * seconds)))


def live_encoder_props(name, fps, keyframe_s=None, x264_preset=None):
    """The per-encoder property set for a LIVE, loss-tolerant stream, as an ordered (prop, value) list.
    Pure (no Gst) so the policy is unit-testable; _configure_live_encoder applies it defensively.

    Keyframe interval: the encoders' defaults are tuned for files, not lossy links -- x264enc's
    key-int-max=0 means ~250 frames, which at a 10 fps preview is one IDR every 25 s. On WebRTC a
    lost keyframe packet that NACK/RTX doesn't recover freezes the browser's decoder until the NEXT
    IDR (PLI recovery exists but is best-effort) -- and the dashboard's stall watchdog tears the
    session down at 12 s, well before it. A ~2 s GOP bounds that freeze; the bitrate cost at these
    rates is small (default CAM_WEBRTC_KEYFRAME_S=2.0; 0 leaves the encoder default).

    x264enc preset: `medium` (the element default) is the software FALLBACK path -- a CPU-bound host
    (no NVENC, x86 dev box, replay on a laptop) falls behind at 5MP-debayered input, the leaky queue
    ahead of the encoder drops, and output stalls. `superfast` cuts encode cost several-fold for a
    preview-quality loss (CAM_WEBRTC_X264_PRESET; the nick names of x264enc's speed-preset enum)."""
    keyframe_s = _KEYFRAME_S_DEFAULT if keyframe_s is None else keyframe_s
    try:
        keyframe_s = float(keyframe_s)
    except (TypeError, ValueError):
        keyframe_s = _KEYFRAME_S_DEFAULT
    gop = keyframe_interval_frames(fps, keyframe_s) if keyframe_s > 0 else None
    props = []
    if name == "x264enc":
        props.append(("speed-preset", (x264_preset or _X264_PRESET_DEFAULT).strip().lower()))
        if gop is not None:
            props.append(("key-int-max", gop))
    elif name in ("nvv4l2h264enc", "nvv4l2h265enc"):
        if gop is not None:
            props.append(("iframeinterval", gop))
    elif name in ("openh264enc", "nvh264enc", "vaapih264enc", "vah264enc"):
        if gop is not None:
            props.append(("gop-size", gop))
    return props
