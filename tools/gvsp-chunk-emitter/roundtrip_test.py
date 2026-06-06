"""Full input->output round-trip over GVSP, compared against the EXACT bytes we transmit.

Builds N self-identifying GRAY8 frames -- each stamps its own index (bytes 0:8) and
injected timestamp (bytes 8:16) over the frame content. The content is either:
  * default: deterministic random noise seeded by index -- near-incompressible, so it's a
    real lossless test the codec can't cheat; or
  * --input <file>: a real video/image file, decoded + scaled to GRAY8 512x512 (cycled if
    it has fewer than N frames), so you can round-trip actual footage through the pipeline.

The stamped frames are written to a replay file; the patched emitter sends frame i with
ChunkTimestamp = ts[i]; our pipeline records a lossless FFV1 mkv + sidecar CSV.

Then it decodes the recording and verifies, per recorded frame:
  * the decoded pixels are bit-exact vs. THE EXACT BYTES WE SENT -- read back from the
    replay file at the embedded index (not regenerated), so the only assumption is that
    the emitter transmitted the file faithfully (which this is precisely testing); and
  * the sidecar chunk_ns equals the timestamp embedded in that frame (timestamp fidelity).

Self-aligning: each frame carries its index, so dropped/missed frames don't desync the
comparison. Run inside the cam-chunks container (see roundtrip_test.sh).
"""
import argparse
import csv
import glob
import os
import random
import subprocess
import sys
import time

IMG_W, IMG_H = 512, 512
IMG = IMG_W * IMG_H              # GRAY8 bytes per frame (must match the fake camera geometry)
N = 400                         # plenty so a ~6 s run never loops (=> monotonic timestamps)
BASE = 1_700_000_000_000_000_000
PERIOD = 40_000_000             # 25 fps, in ns
FRAMES, TS = "/tmp/gt_frames.raw", "/tmp/gt_timestamps.txt"
DECODED_IN = "/tmp/input_decoded.raw"
RECDIR = "/data/recordings"


def stamp(content: bytes, i: int) -> bytes:
    """Overwrite [0:8]=index and [8:16]=timestamp so each frame self-identifies for alignment."""
    f = bytearray(content)
    f[0:8] = i.to_bytes(8, "little")
    f[8:16] = (BASE + i * PERIOD).to_bytes(8, "little")
    return bytes(f)


def noise_content(i: int) -> bytes:
    # deterministic per-frame RANDOM NOISE seeded by index -- near-incompressible, so any
    # lossy behaviour corrupts it and the codec can't cheat.
    return random.Random(i).randbytes(IMG)


def decode_input(path: str) -> list:
    """Decode any gst-readable file to GRAY8 512x512 raw frames (a list of IMG-byte frames).
    The source codec/size/aspect don't matter -- we just need frames to push through."""
    subprocess.run(
        ["gst-launch-1.0", "-q", "filesrc", f"location={path}", "!", "decodebin", "!",
         "videoconvert", "!", "videoscale", "!",
         f"video/x-raw,format=GRAY8,width={IMG_W},height={IMG_H}", "!",
         "filesink", f"location={DECODED_IN}"], check=True)
    nf = os.path.getsize(DECODED_IN) // IMG
    if nf == 0:
        raise SystemExit(f"input {path} decoded to 0 frames at {IMG_W}x{IMG_H} GRAY8")
    with open(DECODED_IN, "rb") as f:
        frames = [f.read(IMG) for _ in range(nf)]
    print(f"decoded input {os.path.basename(path)} -> {nf} GRAY8 {IMG_W}x{IMG_H} frames "
          f"(cycled to N={N})")
    return frames


def generate(source) -> None:
    """source(i) -> content bytes for frame i; stamp + write the replay + timestamp files."""
    with open(FRAMES, "wb") as ff, open(TS, "w") as tf:
        for i in range(N):
            ff.write(stamp(source(i), i))
            tf.write(f"{BASE + i * PERIOD}\n")
    print(f"prepared {N} ground-truth frames ({IMG} B each) + timestamps")


def main() -> int:
    ap = argparse.ArgumentParser(description="GVSP round-trip vs the exact transmitted bytes")
    ap.add_argument("--input", help="video/image file to round-trip (decoded to GRAY8 512x512); "
                                     "default = deterministic random noise")
    args = ap.parse_args()

    if args.input:
        decoded_in = decode_input(args.input)
        source = lambda i: decoded_in[i % len(decoded_in)]   # cycle content; index/ts stay unique per i
    else:
        source = noise_content

    os.makedirs(RECDIR, exist_ok=True)
    for f in glob.glob(f"{RECDIR}/rt*"):
        os.remove(f)
    generate(source)

    env = dict(os.environ, ARV_REPLAY_FRAMES=FRAMES, ARV_REPLAY_TIMESTAMPS=TS)
    emit = subprocess.Popen(["arv-fake-gv-camera-0.8", "-i", "127.0.0.1", "-s", "GV01"],
                            env=env, stdout=open("/tmp/emit.log", "wb"), stderr=subprocess.STDOUT)
    time.sleep(3)
    core = subprocess.Popen(["python3", "main.py", "-c", "config/gvsp-roundtrip.yaml"],
                            stdout=open("/tmp/core.log", "wb"), stderr=subprocess.STDOUT)
    time.sleep(6)
    core.send_signal(2)                 # SIGINT -> clean stop + finalize recording
    core.wait(timeout=20)
    emit.terminate()

    mkvs = sorted(glob.glob(f"{RECDIR}/rt-*.mkv"))
    if not mkvs:
        print("FAIL: no recording produced"); return 1
    decoded_out = "/tmp/decoded.raw"
    subprocess.run(["gst-launch-1.0", "-q", "filesrc", f"location={mkvs[0]}", "!",
                    "matroskademux", "!", "avdec_ffv1", "!", "videoconvert", "!",
                    "video/x-raw,format=GRAY8", "!", "filesink", f"location={decoded_out}"], check=True)

    rows = list(csv.DictReader(open(f"{RECDIR}/rt.csv")))
    n_out = os.path.getsize(decoded_out) // IMG
    n = min(n_out, len(rows))
    mkv_sz = os.path.getsize(mkvs[0])
    print(f"decoded {n_out} frames, sidecar {len(rows)} rows -> comparing {n}")
    print(f"FFV1 recording: {mkv_sz} B = {mkv_sz / max(1, n_out) / IMG:.2f}x raw")

    ok_px = ok_ts = 0
    seen = set()
    with open(decoded_out, "rb") as df, open(FRAMES, "rb") as gf:
        for j in range(n):
            frame = df.read(IMG)
            i = int.from_bytes(frame[0:8], "little")
            ts_embed = int.from_bytes(frame[8:16], "little")
            seen.add(i)
            gf.seek(i * IMG)                      # the EXACT bytes we transmitted for frame i
            expected = gf.read(IMG)
            if frame == expected:
                ok_px += 1
            elif ok_px == j:
                print(f"  frame {j}: PIXEL MISMATCH (embedded index {i})")
            chunk_ns = int(rows[j]["chunk_ns"]) if rows[j]["chunk_ns"] else -1
            if chunk_ns == ts_embed == BASE + i * PERIOD:
                ok_ts += 1
            elif ok_ts == j:
                print(f"  frame {j}: TS MISMATCH chunk_ns={chunk_ns} embedded={ts_embed} "
                      f"expect={BASE + i * PERIOD}")

    print(f"=== {ok_px}/{n} frames bit-exact (lossless), {ok_ts}/{n} timestamps match, "
          f"{len(seen)} distinct input frames seen ===")
    ok = n > 0 and ok_px == n and ok_ts == n
    print("ROUND-TRIP PASS" if ok else "ROUND-TRIP FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
