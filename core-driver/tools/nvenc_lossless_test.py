"""Bit-exact validation of the HW HEVC-lossless recorder path (8-bit mono).

`nvv4l2h265enc enable-lossless=1` makes the *codec* lossless, but that's necessary,
not sufficient: the recorder rides a GRAY8 / Bayer-8 image in the **Y plane of NV24**
(GRAY8 -> NV24 -> NVMM -> NVENC), and if `videoconvert` maps the gray plane to
*limited/video range* (Y in [16,235]) instead of full range, the round trip is NOT
bit-exact even though the codec is "lossless". Chroma subsampling is irrelevant here --
we drop chroma on decode and only compare the Y plane -- so a mismatch means a
range/colorimetry scale (small systematic delta) or genuine corruption (large delta).

GEOMETRY MATTERS: unpinned, GStreamer picks the YUV side's default colorimetry by height --
limited range above 576 rows. A 512x512 run therefore passed while every 5 MP recording lost
up to 3 codes per pixel. The default is a 5 MP class geometry; pass --geometry for the sensor's.
The recorder now pins full range (formats.LUMA_COLORIMETRY, mirrored below) and replay re-asserts
it on decode, so this checks the pin survives nvvidconv + NVENC.

This pushes known, near-incompressible random GRAY8 frames through the EXACT recorder
encode fragment (see cam_driver/recorder.py: build_recorder_description, hw-hevc-lossless),
decodes them back to GRAY8, and compares byte-for-byte against the bytes we fed in.

BOTH FEEDS are qualified (--feed): `videoconvert`, where the CPU builds the NV24 frame, and `nv12`,
where the recorder hands the encoder the plane plus constant half-resolution chroma and the VIC makes
the NV24 (recording.nvenc_feed; about half the CPU). Whether the VIC passes a plane through verbatim
depends on the input format and the L4T release -- on R39 NV12 does and I420 does not -- so the
service proves the NV12 feed itself at start-up and falls back to videoconvert when the proof fails.
The verdict here follows that: videoconvert failing is a FAIL (nothing exact is left to fall back
to); nv12 failing alone is reported and passes, because the service will not use it on this host.
Ask for one feed by name to make it decide the verdict (do so before setting nvenc_feed: nv12).

Runs inside the core image WITH the NVENC CDI device (see docs/jetpack7-bringup.md):
    docker run --rm --device nvidia.com/gpu=all cam-core tools/nvenc_lossless_test.py
(or via tools/nvenc_lossless_test.sh). Everything stays in the container's /tmp -- no
bind mounts, no root-owned artefacts on the host. PASS => the HW path is mathematically
lossless for 8-bit mono; FAIL with small systematic deltas => the full-range pin did not
survive nvvidconv/NVENC on this L4T (a range/colorimetry scale, not codec loss).
"""
import argparse
import os
import random
import subprocess
import sys

W, H = 2448, 2048                 # 5 MP class GigE geometry (> 576 rows; see the docstring)
IMG = W * H                       # GRAY8 bytes/frame
LUMA_COLORIMETRY = "1:4:0:0"      # mirrors cam_driver/formats.py (this script runs standalone)
IN, ENC, OUT = "/tmp/nv_in.raw", "/tmp/nv_enc.mkv", "/tmp/nv_out.raw"
FEEDS = ("videoconvert", "nv12")
# cam_driver/recorder.py _hw_hevc_lossless with the default recording config, token for token
# (tests/test_nvenc_validation.py holds the two together).
HW_HEVC_LOSSLESS = ["nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV24", "!",
                    "nvv4l2h265enc", "enable-lossless=1", "maxperf-enable=1", "!", "h265parse"]


def feed_input(feed):
    """The raw file the feed's encode reads: GRAY8 frames, or for nv12 each frame followed by its
    constant chroma -- the two blocks the recorder's feeder pushes as one buffer."""
    return IN if feed == "videoconvert" else IN + ".nv12"


def encode_argv(feed="videoconvert"):
    # EXACT recorder hw-hevc-lossless fragment, fed from a raw file instead of appsrc.
    if feed == "nv12":
        head = ["rawvideoparse", "format=nv12", f"width={W}", f"height={H}", "framerate=25/1", "!",
                "capssetter", f"caps=video/x-raw,colorimetry=(string){LUMA_COLORIMETRY}"]
    else:
        head = ["rawvideoparse", "format=gray8", f"width={W}", f"height={H}", "framerate=25/1", "!",
                "videoconvert", "n-threads=1", "!", f"video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY}"]
    return (["gst-launch-1.0", "-q", "filesrc", f"location={feed_input(feed)}", "!"] + head + ["!"]
            + HW_HEVC_LOSSLESS + ["!", "matroskamux", "!", "filesink", f"location={ENC}"])


def decode_argv():
    # Decode HEVC back to GRAY8 (drops chroma -> Y plane = the mono image we encoded), re-asserting
    # full range on the decoded caps the way replay does (sources/replay.py raw_decode_chain).
    return ["gst-launch-1.0", "-q",
            "filesrc", f"location={ENC}", "!", "matroskademux", "!",
            "h265parse", "!", "avdec_h265", "!",
            "capssetter", f"caps=video/x-raw,colorimetry=(string){LUMA_COLORIMETRY}", "!", "videoconvert", "!",
            "video/x-raw,format=GRAY8", "!", "filesink", f"location={OUT}"]


def main() -> int:
    global W, H, IMG
    ap = argparse.ArgumentParser(description="bit-exact check for the HW HEVC-lossless recorder path")
    ap.add_argument("--frames", type=int, default=24, help="number of random GRAY8 frames (default 24)")
    ap.add_argument("--geometry", help=f"WxH of the test frames (default {W}x{H}); use the sensor's")
    ap.add_argument("--feed", choices=("both",) + FEEDS, default="both",
                    help="recorder feed to qualify (default both: videoconvert decides, nv12 is reported)")
    args = ap.parse_args()
    n_in = args.frames
    if n_in <= 0:
        ap.error("--frames must be positive")
    if args.geometry:
        try:
            W, H = (int(v) for v in args.geometry.lower().split("x"))
        except ValueError:
            ap.error("--geometry must be WxH, e.g. 2448x2048")
        if W <= 0 or H <= 0:
            ap.error("--geometry must be positive")
        IMG = W * H
    print(f"geometry {W}x{H}, {n_in} frames")

    feeds = FEEDS if args.feed == "both" else (args.feed,)
    if "nv12" in feeds and (W % 4 or H % 2):
        if args.feed == "nv12":
            ap.error("--feed nv12 needs width % 4 == 0 and an even height (the recorder refuses it too)")
        print(f"nv12 feed skipped: {W}x{H} needs row padding; the recorder uses videoconvert here")
        feeds = ("videoconvert",)
    with open(IN, "wb") as f:                          # near-incompressible ground truth
        for i in range(n_in):
            f.write(random.Random(i).randbytes(IMG))
    if "nv12" in feeds:
        chroma = bytes([128]) * (IMG // 2)
        with open(IN, "rb") as fi, open(feed_input("nv12"), "wb") as fo:
            for _ in range(n_in):
                fo.write(fi.read(IMG))
                fo.write(chroma)

    verdicts = {}
    for feed in feeds:
        for path in (ENC, OUT):                        # never judge a feed by the previous one's files
            if os.path.exists(path):
                os.remove(path)
        print(f"--- feed: {feed}")
        verdicts[feed] = roundtrip(feed, n_in)
    if len(feeds) > 1 and verdicts["videoconvert"] and not verdicts["nv12"]:
        print("nv12 feed is NOT bit-exact on this host: the service's start-up probe will keep it on "
              "videoconvert (do not set recording.nvenc_feed: nv12 here)")
    passed = verdicts[feeds[0]] if len(feeds) == 1 else verdicts["videoconvert"]
    print("NVENC LOSSLESS PASS (bit-exact, all frames)" if passed else "NVENC LOSSLESS FAIL")
    return 0 if passed else 1


def roundtrip(feed, n_in) -> bool:
    for stage, argv in (("encode", encode_argv(feed)), ("decode", decode_argv())):
        r = subprocess.run(argv, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True)
        if r.returncode != 0:
            print(r.stdout)
            print(f"FAIL: gst-launch exited {r.returncode} ({stage})")
            return False

    out_bytes = os.path.getsize(OUT)
    n_out = out_bytes // IMG
    comp = min(n_in, n_out)
    ok = worst = worst_frame = 0
    worst_frame = -1
    with open(IN, "rb") as fi, open(OUT, "rb") as fo:
        for j in range(comp):
            a, b = fi.read(IMG), fo.read(IMG)
            if a == b:
                ok += 1
            else:
                d = max(abs(x - y) for x, y in zip(a, b))
                if d > worst:
                    worst, worst_frame = d, j

    enc_sz = os.path.getsize(ENC)
    print(f"frames: in={n_in} decoded={n_out} compared={comp}")
    print(f"encoded: {enc_sz} B = {enc_sz / max(1, n_out) / IMG:.3f}x raw "
          f"(noise is incompressible, so lossless ~>= 1.0x; a tiny ratio would mean lossy)")
    print(f"=== {ok}/{comp} frames bit-exact, {n_out}/{n_in} survived encode ===")
    lossless = ok == comp and comp > 0
    all_frames = out_bytes == n_in * IMG
    if not lossless:
        kind = "range/colorimetry scale (systematic small delta)" if 0 < worst <= 20 else "corruption"
        print(f"worst mismatch: frame {worst_frame}, max |delta| = {worst}  -> likely {kind}")
    if not all_frames:
        print(f"FAIL: expected {n_in * IMG} decoded bytes, got {out_bytes}; "
              "missing/extra frames or a partial frame invalidate the roundtrip")
    print(f"feed {feed}: " + ("bit-exact, all frames" if lossless and all_frames else "NOT bit-exact"))
    return lossless and all_frames


if __name__ == "__main__":
    sys.exit(main())
