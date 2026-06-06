# GVSP chunk emitter (patched Aravis fake camera)

The stock Aravis fake camera can't emit GenICam **chunk data** (its payload is hardcoded to
IMAGE), so it can't exercise a chunk-parsing pipeline. This tooling patches Aravis to emit,
over **real GVSP on localhost**, two chunks per frame:

- `ChunkTimestamp` (ChunkID `0xa001`, 8 bytes BE) = the buffer timestamp (system time, ns)
- `ChunkFrameID`   (ChunkID `0xa002`, 8 bytes BE) = the frame id

This validates the pipeline's **real Aravis chunk-parse path** with no real camera. (It covers
the Aravis chunk *mechanics*; a real camera's exact PTP/chunk behaviour still needs the hardware.)

## What the patch does (`apply_chunk_patch.py`)

Four edits to Aravis 0.8.36, gated on a new `ChunkModeActive` register (0x140):
- `get_payload` → image **+ 32 bytes** when chunk mode is on (so buffers are sized for chunks)
- `fill_buffer` → append the two chunks in GenICam layout `[value][id:u32_BE][size:u32_BE]` and
  set `payload_type = EXTENDED_CHUNK_DATA`
- `arvgvfakecamera.c` → flag the GVSP leader as image+chunks (`payload_type | 0x4000`) so the
  receiver sets `has_chunks`
- `arv-fake-camera.xml` → `ChunkDataControl` nodes (ChunkModeActive/Selector/Enable, plus
  `ChunkTimestamp`/`ChunkFrameID` `IntReg`s with `ChunkID` ports) + a chunk-aware `PayloadSize`

## Build & run

```bash
docker build -f tools/gvsp-chunk-emitter/Dockerfile -t cam-chunks .   # patched Aravis + our code
./tools/gvsp-chunk-emitter/gvsp_test.sh                                  # chunk extraction over GVSP
./tools/gvsp-chunk-emitter/roundtrip_test.sh [video]                     # round-trip vs exact bytes (noise, or a real file)
./tools/gvsp-chunk-emitter/reconnect_test.sh                             # camera reconnect/backoff (kill + restart emitter)
```

The patch also supports **frame replay** via env vars (`ARV_REPLAY_FRAMES` = raw GRAY8 frames,
`ARV_REPLAY_TIMESTAMPS` = one ns per line): `fill_buffer` then sends those exact frames with those
ChunkTimestamps instead of the generated test pattern. That powers the round-trip test below.

`gvsp_test.sh` starts the emitter (`arv-fake-gv-camera-0.8 -i 127.0.0.1`), runs
`chunk_check.py` (direct chunk read) and then the real pipeline (`core-driver/config/gvsp-test.yaml`),
and checks `ChunkTimestamp` extraction (`source=ptp_chunk`) + that the FFV1 recording decodes.

## Validated

Receiver reads `payload=262176` (262144 image + 32 chunk bytes), `has_chunks=True`,
`ChunkTimestamp`/`ChunkFrameID` match the injected values, the pipeline records losslessly, and
the `system_ns − chunk_ns` column shows the real arrival latency/jitter (~2–7 ms, variable) that
the hardware timestamp avoids. It also caught a real bug: `arv_buffer_get_data()` returns
image+chunks, so the pipeline now slices to the image size before encoding.

## Round-trip test (`roundtrip_test.py`)

Builds N self-identifying GRAY8 frames — each stamps its index + injected timestamp over the
content, which is either **deterministic random noise** (seeded by index — near-incompressible, so
the codec can't cheat) or, with `roundtrip_test.sh <video>`, a **real decoded video** (any
gst-decodable file, scaled to GRAY8 512×512 and cycled to N). Replays them through the emitter,
records via the real pipeline, then decodes the recording and verifies **per frame**: decoded pixels
are bit-exact vs. **the exact bytes we transmitted** — read back from the replay file at the embedded
index, not regenerated — AND the sidecar `chunk_ns` equals the embedded/injected timestamp.
Self-aligning by embedded index, so dropped frames don't desync.

Result: **bit-exact frames + matching timestamps** on both inputs — random noise records at ~1.02×
raw (incompressible, so that ratio proves full entropy was stored losslessly), and a real clip at
~0.01× (highly compressible) yet still perfectly lossless. Known ground truth, real GVSP, no hardware.
