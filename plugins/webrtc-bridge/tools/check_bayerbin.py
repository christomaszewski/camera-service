"""The compiled `bayerbin` element (gst/gstbayerbin.c) against frames whose answer is known; run explicitly
in the WebRTC image (webrtc_test.sh does).

Outside pytest's test_*.py unit collection for the reason check_encoder_setup.py is: the unit suite
runs in the core's image, which does not have the element. What the bridge does WITHOUT it (the full
debayer, as before) is unit-tested in test_bridge_sessions.py.
"""
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

PATTERNS = {"rggb": "RG/GB", "bggr": "BG/GR", "grbg": "GR/BG", "gbrg": "GB/RG"}


def mosaic(pattern, width, height, stride=None):
    """A mosaic whose every cell is known: at cell (cx, cy) red = 10 + cx + 16*cy, blue = 200 - cx - 16*cy,
    the green beside red = 100 + 2*cx, the green beside blue = 101 + 2*cy + 2*cx. An odd last row and
    column hold 255, which must never reach the output. `stride` > width pads each row with 0xEE."""
    stride = stride or width
    top, bottom = PATTERNS[pattern].split("/")
    out = bytearray()
    for y in range(height):
        letters = top if y % 2 == 0 else bottom
        row = bytearray()
        for x in range(width):
            cx, cy = x // 2, y // 2
            if 2 * cx + 1 >= width or 2 * cy + 1 >= height:
                row.append(255)                          # the cropped edge
            elif letters[x % 2] == "R":
                row.append(10 + cx + 16 * cy)
            elif letters[x % 2] == "B":
                row.append(200 - cx - 16 * cy)
            else:                                        # green: which one is by the row it is in
                row.append(100 + 2 * cx if "R" in letters else 101 + 2 * cy + 2 * cx)
        out += row + b"\xee" * (stride - width)
    return bytes(out)


def expected(width, height):
    out = bytearray()
    for cy in range(height // 2):
        for cx in range(width // 2):
            green = ((100 + 2 * cx) + (101 + 2 * cy + 2 * cx)) >> 1
            out += bytes((10 + cx + 16 * cy, green, 200 - cx - 16 * cy, 255))
    return bytes(out)


def run(pattern, width, height, frames):
    """Push `frames` (bytes) through bayerbin: (the output caps, each output frame's bytes)."""
    pipeline = Gst.parse_launch("appsrc name=src format=time ! bayerbin name=bin ! appsink name=out sync=false")
    src, out = pipeline.get_by_name("src"), pipeline.get_by_name("out")
    src.set_property("caps", Gst.Caps.from_string(
        "video/x-bayer,format={},width={},height={},framerate=24/1".format(pattern, width, height)))
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for i, data in enumerate(frames):
            buf = Gst.Buffer.new_wrapped(data)
            buf.pts = i * Gst.SECOND // 24
            assert src.emit("push-buffer", buf) == Gst.FlowReturn.OK
        src.emit("end-of-stream")
        caps, got = None, []
        while True:
            sample = out.emit("try-pull-sample", 5 * Gst.SECOND)
            if sample is None:
                break
            caps = sample.get_caps()
            ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
            assert ok
            got.append((sample.get_buffer().pts, bytes(info.data)))
            sample.get_buffer().unmap(info)
        message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        assert message is None, message.parse_error()
        return caps, got
    finally:
        pipeline.set_state(Gst.State.NULL)


def test_every_pattern_gives_its_cells_red_mean_green_and_blue():
    for pattern in PATTERNS:
        caps, got = run(pattern, 8, 6, [mosaic(pattern, 8, 6)])
        st = caps.get_structure(0)
        assert (st.get_name(), st.get_string("format")) == ("video/x-raw", "RGBx"), caps.to_string()
        assert (st.get_int("width")[1], st.get_int("height")[1]) == (4, 3), caps.to_string()
        assert st.get_fraction("framerate")[1:] == (24, 1), caps.to_string()
        assert [data for _, data in got] == [expected(8, 6)], pattern


def test_an_odd_edge_is_cropped():
    for width, height in ((7, 5), (9, 6), (8, 7), (2, 2), (3, 3)):
        caps, got = run("grbg", width, height, [mosaic("grbg", width, height)])
        st = caps.get_structure(0)
        assert (st.get_int("width")[1], st.get_int("height")[1]) == (width // 2, height // 2)
        assert [data for _, data in got] == [expected(width, height)], (width, height)


def test_rows_may_be_tight_or_padded_to_four_bytes():
    # GStreamer pads video/x-bayer rows to four bytes; the camera core sends them tight. Widths that are
    # no multiple of four are where the two differ.
    for width in (6, 7, 10):
        padded = (width + 3) // 4 * 4
        tight, wide = mosaic("rggb", width, 4), mosaic("rggb", width, 4, stride=padded)
        assert len(wide) > len(tight)
        _, got = run("rggb", width, 4, [tight, wide, tight])
        assert [data for _, data in got] == [expected(width, 4)] * 3, width


def test_a_short_frame_is_dropped_and_the_stream_goes_on():
    good = mosaic("bggr", 8, 6)
    _, got = run("bggr", 8, 6, [good, good[:-9], b"\x00", good])
    assert [data for _, data in got] == [expected(8, 6)] * 2
    assert [pts for pts, _ in got] == [0, 3 * Gst.SECOND // 24]          # frames keep their own timestamps


def test_a_real_size_matches_the_same_arithmetic():
    width, height = 2448, 2048                                           # the 5 MP camera; values wrap at 256
    frame = bytes((x * 7 + y * 13) % 256 for y in range(height) for x in range(width))
    _, got = run("rggb", width, height, [frame])
    out = got[0][1]
    assert len(out) == (width // 2) * (height // 2) * 4
    for cx, cy in ((0, 0), (1, 0), (611, 511), (1223, 1023), (1000, 3)):
        at = lambda x, y: frame[y * width + x]                           # noqa: E731
        want = bytes((at(2 * cx, 2 * cy), (at(2 * cx + 1, 2 * cy) + at(2 * cx, 2 * cy + 1)) >> 1,
                      at(2 * cx + 1, 2 * cy + 1), 255))
        assert out[(cy * (width // 2) + cx) * 4:][:4] == want, (cx, cy)


if __name__ == "__main__":
    Gst.init(None)
    assert Gst.ElementFactory.find("bayerbin") is not None, "this image has no bayerbin element"
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print("ok", name)
