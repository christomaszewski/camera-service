"""Tests for the recorder's encoder-availability fallback + the x265 depth guard.

GStreamer element lookup is monkeypatched (rec.Gst), so every inventory case runs
anywhere the gi bindings import -- no NVENC/x265/libav plugins (or hardware) needed.
If gi itself is absent (bare host), the suite SKIPs; the dev container covers it.

Run: python3 core-driver/tests/test_recorder.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import cam_driver.recorder as rec
    from cam_driver.config import RecordingConfig
except ImportError as e:   # no gi/GStreamer on this host
    if "pytest" in sys.modules:
        # Under pytest a module-level sys.exit() aborts COLLECTION for the whole suite
        # (INTERNALERROR), taking every other test file down with it -- skip properly.
        import pytest
        pytest.skip(f"recorder needs gi/GStreamer: {e}", allow_module_level=True)
    print(f"SKIP: {e}")   # standalone `python3 test_recorder.py` run
    sys.exit(0)


class _FakeGst:
    """Just enough Gst for recorder.py: SECOND + ElementFactory.find over a fixed inventory."""
    SECOND = 1_000_000_000

    def __init__(self, present):
        inventory = set(present)

        class ElementFactory:
            @staticmethod
            def find(name):
                return name if name in inventory else None

        self.ElementFactory = ElementFactory


_JETSON = {"nvvidconv", "nvv4l2h265enc", "x265enc", "avenc_ffv1"}
_DEV = {"x265enc", "avenc_ffv1"}          # x86 / container without the L4T stack
_BARE = set()                             # no optional codec packages at all


def _build(present, encoder="auto", bits=8, is_color=False, parser=None):
    """-> (launch fragment, RESOLVED encoder). The resolved encoder is what pipeline.build() wires
    the recorder from, so it is part of the contract, not an implementation detail."""
    rec.Gst = _FakeGst(present)
    return rec.build_recorder_description(
        RecordingConfig(encoder=encoder), bits, "/data/recordings/t", 25.0, is_color, parser)


def _desc(present, encoder="auto", bits=8, is_color=False, parser=None):
    return _build(present, encoder, bits, is_color, parser)[0]


def test_auto_8bit_uses_nvenc_when_present():
    d = _desc(_JETSON)
    assert "nvv4l2h265enc enable-lossless=1" in d


def test_auto_8bit_falls_back_to_ffv1_without_nvenc():
    # the x86/dev-container case that used to die inside Gst.parse_launch
    d = _desc(_DEV)
    assert "avenc_ffv1" in d and "nvv4l2h265enc" not in d


def test_explicit_hw_request_also_falls_back():
    d = _desc(_DEV, encoder="hw-hevc-lossless")
    assert "avenc_ffv1" in d and "nvv4l2h265enc" not in d


def test_x265_missing_falls_back():
    d = _desc({"avenc_ffv1"}, encoder="x265-lossless")
    assert "avenc_ffv1" in d and "x265enc" not in d


def test_x265_8bit_mono_pins_i420():
    # mono/Bayer must ride bit-exact in the Y plane -- the caps pin keeps videoconvert honest
    d = _desc(_DEV, encoder="x265-lossless")
    assert "video/x-raw,format=I420 ! x265enc" in d


def test_x265_8bit_color_left_unpinned():
    # color: an I420 pin would chroma-resample 4:2:2/4:4:4/RGB sources, so no pin is emitted
    d = _desc(_DEV, encoder="x265-lossless", is_color=True)
    assert "x265enc" in d and "format=I420" not in d


def test_x265_gray16_depth_guard():
    # >8-bit rides in GRAY16; x265 formats top out at 12-bit -> fall back, never drop real bits
    d = _desc(_JETSON, encoder="x265-lossless", bits=16)
    assert "avenc_ffv1" in d and "x265enc" not in d


def test_hw_hevc_gray16_depth_guard():
    # Same constraint, other encoder: the Orin has NO 10/12-bit HW lossless, and
    # `videoconvert ! NV24` happily negotiates from GRAY16_LE while keeping only the HIGH byte -- so
    # a pinned hw-hevc-lossless on a 16-bit radiometric camera silently threw away the low 8 bits
    # while the sidecar still attested bits_per_pixel: 16. _JETSON has the NVENC elements, so the
    # availability probe cannot be what makes this pass.
    d, enc = _build(_JETSON, encoder="hw-hevc-lossless", bits=16)
    assert enc == "ffv1"
    assert "avenc_ffv1" in d and "nvv4l2h265enc" not in d and "NV24" not in d


def test_ffv1_encodes_threaded():
    # avenc_ffv1 defaults to ONE thread, which caps 16-bit 640x512 at ~26-29 fps on an Orin core --
    # a 60 fps thermal camera stalled the recorder in the field (tee blocked, consumers starved).
    # FFV1 parallelizes across slices; these knobs must stay pinned (lossless is unaffected).
    d = _desc(_JETSON, encoder="ffv1", bits=16)
    assert "avenc_ffv1" in d and "threads=0" in d and "slices=4" in d and "slicecrc=1" in d


def test_no_lossless_encoder_at_all_raises():
    try:
        _desc(_BARE, encoder="ffv1")
    except RuntimeError as e:
        assert "avenc_ffv1" in str(e)
    else:
        raise AssertionError("expected RuntimeError when no encoder element exists")


def test_stream_copy_needs_no_encoder_elements():
    # stream-copy is parser + mux only; the probe must not block it on a bare host
    d, enc = _build(_BARE, parser="h264parse")
    assert enc == "stream-copy"
    assert "h264parse" in d and "rec_q" in d


def test_stream_copy_on_a_raw_source_resolves_to_ffv1():
    # The one cell of the encoder x source-kind matrix that had no coverage, and it recorded NOTHING:
    # pipeline.build() used to re-derive "is this stream-copy?" from select_encoder alone, which
    # honors an explicit `stream-copy` regardless of the source, while the recorder had already
    # fallen back to ffv1 here. The pipeline then dropped the tee branch and hung the ffv1 fragment
    # off an encoded appsrc that a raw source never feeds. The RESOLVED encoder is what breaks the
    # tie, so it has to say ffv1 -- and the fragment must be a tee-linkable re-encode, not a copy.
    d, enc = _build(_DEV, encoder="stream-copy", parser=None)
    assert enc == "ffv1"
    assert "avenc_ffv1" in d and "videoconvert" in d


def test_resolved_encoder_reflects_availability_fallback():
    # The resolved encoder must survive the availability probe too -- pipeline.build() wires from
    # this value, so a hw request degraded to ffv1 on a non-Jetson must report ffv1, not the request.
    _d, enc = _build(_DEV, encoder="hw-hevc-lossless")
    assert enc == "ffv1"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
