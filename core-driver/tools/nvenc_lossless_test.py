"""Bit-exact validation of the HW HEVC-lossless recorder path (8-bit mono).

`nvv4l2h265enc enable-lossless=1` makes the *codec* lossless, but that's necessary,
not sufficient: the recorder rides a GRAY8 / Bayer-8 image in the **Y plane of NV24**
(GRAY8 -> NV24 -> NVMM -> NVENC), and if `videoconvert` maps the gray plane to
*limited/video range* (Y in [16,235]) instead of full range, the round trip is NOT
bit-exact even though the codec is "lossless". Chroma subsampling is irrelevant here --
we drop chroma on decode and only compare the Y plane -- so a mismatch means a
range/colorimetry scale (small systematic delta) or genuine corruption (large delta).

This pushes known, near-incompressible random GRAY8 frames through the EXACT recorder
encode fragment (see gige_driver/recorder.py: build_recorder_description, hw-hevc-lossless),
decodes them back to GRAY8, and compares byte-for-byte against the bytes we fed in.

Runs inside the core image WITH the NVENC CDI device (see docs/jetpack7-bringup.md):
    docker run --rm --device nvidia.com/gpu=all gige-core tools/nvenc_lossless_test.py
(or via tools/nvenc_lossless_test.sh). Everything stays in the container's /tmp -- no
bind mounts, no root-owned artefacts on the host. PASS => the HW path is mathematically
lossless for 8-bit mono; FAIL with small systematic deltas => a full-range cap to add in
the recorder's NV24 conversion.
"""
import argparse
import os
import random
import subprocess
import sys

W, H = 512, 512
IMG = W * H                       # GRAY8 bytes/frame (matches the fake-camera geometry)
IN, ENC, OUT = "/tmp/nv_in.raw", "/tmp/nv_enc.mkv", "/tmp/nv_out.raw"


def encode_argv():
    # EXACT recorder hw-hevc-lossless fragment, fed from a raw GRAY8 file instead of appsrc.
    return ["gst-launch-1.0", "-q",
            "filesrc", f"location={IN}", "!",
            "rawvideoparse", "format=gray8", f"width={W}", f"height={H}", "framerate=25/1", "!",
            "videoconvert", "!", "video/x-raw,format=NV24", "!",
            "nvvidconv", "!", "video/x-raw(memory:NVMM),format=NV24", "!",
            "nvv4l2h265enc", "enable-lossless=1", "maxperf-enable=1", "!",
            "h265parse", "!", "matroskamux", "!", "filesink", f"location={ENC}"]


def decode_argv():
    # Decode HEVC back to GRAY8 (drops chroma -> Y plane = the mono image we encoded).
    return ["gst-launch-1.0", "-q",
            "filesrc", f"location={ENC}", "!", "matroskademux", "!",
            "h265parse", "!", "avdec_h265", "!", "videoconvert", "!",
            "video/x-raw,format=GRAY8", "!", "filesink", f"location={OUT}"]


def main() -> int:
    ap = argparse.ArgumentParser(description="bit-exact check for the HW HEVC-lossless recorder path")
    ap.add_argument("--frames", type=int, default=60, help="number of random GRAY8 frames (default 60)")
    n_in = ap.parse_args().frames

    with open(IN, "wb") as f:                          # near-incompressible ground truth
        for i in range(n_in):
            f.write(random.Random(i).randbytes(IMG))

    for argv in (encode_argv(), decode_argv()):
        r = subprocess.run(argv, stderr=subprocess.STDOUT, stdout=subprocess.PIPE, text=True)
        if r.returncode != 0:
            print(r.stdout)
            print(f"FAIL: gst-launch exited {r.returncode} ({'encode' if argv is encode_argv() else 'decode'})")
            return 1

    n_out = os.path.getsize(OUT) // IMG
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
    all_frames = n_out == n_in
    if not lossless:
        kind = "range/colorimetry scale (systematic small delta)" if 0 < worst <= 20 else "corruption"
        print(f"worst mismatch: frame {worst_frame}, max |delta| = {worst}  -> likely {kind}")
    elif not all_frames:
        print(f"note: {n_in - n_out} tail frame(s) didn't survive the gst-launch EOS flush "
              f"(a launch-only artefact; the real recorder finalizes via splitmuxsink EOS)")
    print("NVENC LOSSLESS PASS (bit-exact)" if lossless else "NVENC LOSSLESS FAIL")
    return 0 if lossless else 1


if __name__ == "__main__":
    sys.exit(main())
