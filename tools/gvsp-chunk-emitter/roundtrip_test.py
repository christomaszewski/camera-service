"""Full input->output round-trip over GVSP, against known ground truth.

Generates N self-identifying GRAY8 frames -- each frame embeds its own index (bytes
0:8) and injected timestamp (bytes 8:16), the rest a deterministic pattern -- plus a
matching timestamp file. The patched emitter replays them (frame i sent with
ChunkTimestamp = ts[i]); our pipeline receives over GVSP, extracts the chunk timestamp,
and records a lossless FFV1 mkv + sidecar CSV.

Then it decodes the recording and verifies, per recorded frame:
  * the decoded pixels are bit-exact vs. the regenerated input frame (lossless), and
  * the sidecar chunk_ns equals the timestamp embedded in that frame (timestamp fidelity).

Self-aligning: each frame carries its index, so dropped/missed frames don't desync the
comparison. Run inside the gige-chunks container (see roundtrip_test.sh).
"""
import csv
import glob
import os
import random
import subprocess
import sys
import time

IMG_W, IMG_H = 512, 512
IMG = IMG_W * IMG_H              # GRAY8 bytes per frame
N = 400                         # plenty so a ~6 s run never loops (=> monotonic timestamps)
BASE = 1_700_000_000_000_000_000
PERIOD = 40_000_000             # 25 fps, in ns
FRAMES, TS = "/tmp/gt_frames.raw", "/tmp/gt_timestamps.txt"
RECDIR = "/data/recordings"


def gen_frame(i: int) -> bytes:
    # deterministic per-frame RANDOM NOISE (seeded by index) -- near-incompressible, so this
    # is a real lossless test: any lossy behaviour corrupts it and the codec can't cheat.
    f = bytearray(random.Random(i).randbytes(IMG))
    f[0:8] = i.to_bytes(8, "little")
    f[8:16] = (BASE + i * PERIOD).to_bytes(8, "little")
    return bytes(f)


def generate():
    with open(FRAMES, "wb") as ff, open(TS, "w") as tf:
        for i in range(N):
            ff.write(gen_frame(i))
            tf.write(f"{BASE + i * PERIOD}\n")
    print(f"generated {N} ground-truth frames ({IMG} B each) + timestamps")


def main() -> int:
    os.makedirs(RECDIR, exist_ok=True)
    for f in glob.glob(f"{RECDIR}/rt*"):
        os.remove(f)
    generate()

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
    decoded = "/tmp/decoded.raw"
    subprocess.run(["gst-launch-1.0", "-q", "filesrc", f"location={mkvs[0]}", "!",
                    "matroskademux", "!", "avdec_ffv1", "!", "videoconvert", "!",
                    "video/x-raw,format=GRAY8", "!", "filesink", f"location={decoded}"], check=True)

    rows = list(csv.DictReader(open(f"{RECDIR}/rt.csv")))
    n_out = os.path.getsize(decoded) // IMG
    n = min(n_out, len(rows))
    mkv_sz = os.path.getsize(mkvs[0])
    print(f"decoded {n_out} frames, sidecar {len(rows)} rows -> comparing {n}")
    print(f"FFV1 recording: {mkv_sz} B = {mkv_sz / max(1, n_out) / IMG:.2f}x raw "
          f"(random noise barely compresses -> real entropy was stored losslessly)")

    ok_px = ok_ts = 0
    seen = set()
    with open(decoded, "rb") as df:
        for j in range(n):
            frame = df.read(IMG)
            i = int.from_bytes(frame[0:8], "little")
            ts_embed = int.from_bytes(frame[8:16], "little")
            seen.add(i)
            if frame == gen_frame(i):
                ok_px += 1
            elif ok_px == j:
                print(f"  frame {j}: PIXEL MISMATCH (embedded index {i})")
            chunk_ns = int(rows[j]["chunk_ns"]) if rows[j]["chunk_ns"] else -1
            if chunk_ns == ts_embed == BASE + i * PERIOD:
                ok_ts += 1
            elif ok_ts == j:
                print(f"  frame {j}: TS MISMATCH chunk_ns={chunk_ns} embedded={ts_embed} expect={BASE + i * PERIOD}")

    print(f"=== {ok_px}/{n} frames bit-exact (lossless), {ok_ts}/{n} timestamps match, "
          f"{len(seen)} distinct input frames seen ===")
    ok = n > 0 and ok_px == n and ok_ts == n
    print("ROUND-TRIP PASS" if ok else "ROUND-TRIP FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
