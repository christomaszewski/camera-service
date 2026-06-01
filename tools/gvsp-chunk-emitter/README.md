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
docker build -f tools/gvsp-chunk-emitter/Dockerfile -t gige-chunks .   # patched Aravis + our code
./tools/gvsp-chunk-emitter/gvsp_test.sh                                  # chunk extraction over GVSP
./tools/gvsp-chunk-emitter/roundtrip_test.sh                            # full input->output round-trip
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

Generates N self-identifying GRAY8 frames — each embeds its index + injected timestamp, the rest
**deterministic random noise** (seeded by index, so it's reproducible but near-incompressible).
Replays them through the emitter, records via the real pipeline, then decodes the recording and
verifies **per frame**: decoded pixels are bit-exact vs. the regenerated input (lossless) AND the
sidecar `chunk_ns` equals the embedded/injected timestamp (timestamp fidelity). Self-aligning by
embedded index, so dropped frames don't desync.

Result: **118/118 frames bit-exact, 118/118 timestamps match**, with the FFV1 recording at ~1.02×
raw size — random noise barely compresses, so that ratio proves full entropy was stored losslessly
(a gradient would compress to a fraction and prove little). Lossless recording + end-to-end timestamp
fidelity, against known ground truth, over real GVSP, no hardware.
