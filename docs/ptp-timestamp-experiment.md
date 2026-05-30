# Experiment: which hardware timestamp to trust (FLIR Blackfly S + PTP)

**Status: TODO — run once a real GigE Vision camera (FLIR Blackfly S) is available.**
The Aravis *fake* camera cannot drive this (no chunk/PTP support), so this must be
done on hardware.

## Why this experiment exists

The whole point of the driver is to stamp each frame with the *true sensor-capture
time* so we don't bake in network + processing latency/jitter. The camera exposes
**three** candidate timestamps per frame, and we need to know — on this specific
camera + firmware — which is the authoritative PTP capture time, how the others
relate to it, and how much arrival-time jitter we're actually avoiding.

The three candidates (all logged every frame — see "Data" below):

| Column | Source | Notes |
|---|---|---|
| `chunk_ns`  | `ChunkTimestamp` from the GVSP chunk trailer (`ArvChunkParser`) | FLIR docs: PTP-synced, **end-of-exposure**. Our **primary**. Tick-converted to ns. |
| `camera_ns` | `arv_buffer_get_timestamp()` — the GVSP **leader-packet** timestamp | Same device clock; PTP when locked. Aravis already converts to ns. Our **fallback**. |
| `system_ns` | `arv_buffer_get_system_timestamp()` — host arrival (`CLOCK_REALTIME`) | What we'd get if we naively timestamped on arrival. The thing we're trying to beat. |

## Prerequisites

1. **Host = PTP grandmaster.** On the Jetson, on the camera NIC:
   ```bash
   sudo ptp4l -i <cam-iface> -m            # serve PTP on the camera network
   sudo phc2sys -a -r                      # discipline the Jetson system clock to the PHC
   ```
   `phc2sys` matters: it puts `system_ns` on the *same* time base as the camera, so
   `system_ns − chunk_ns` becomes a real, comparable latency rather than an arbitrary offset.
2. **Camera config:** `timestamp_source: ptp_chunk`, `ptp_enable: true` (the defaults).
3. **Confirm lock from the startup log:**
   - `PTP GevIEEE1588Status = Slave` (camera slaved to the Jetson grandmaster), and
   - `chunk mode enabled: Timestamp,FrameID`, and
   - `GevTimestampTickFrequency = 1000000000` (1 GHz → ticks == ns under PTP).
   If `Status` never reaches `Slave`, fix PTP first — everything below is meaningless until it does.

## Run + capture

Record ~30–60 s at your target frame rate (adapt `config/camera.yaml`). The startup
log prints the first 5 frames' breakdown for a quick eyeball; the **full per-frame
data is in the sidecar `*.csv`** (columns `chunk_ns, camera_ns, system_ns`), which is
what to analyze. The `*.json` header records the active source and tick frequency.

## Data — what to compute (e.g. pandas on the CSV)

With frame period `T = 1e9 / fps` (ns):

1. **chunk vs leader:** `d_cl = chunk_ns − camera_ns` → mean and std.
2. **arrival latency:** `d_sc = system_ns − chunk_ns` → mean, std, min.
3. **interval stability:** `Δchunk[i] = chunk_ns[i] − chunk_ns[i-1]` (and `Δcamera`, `Δsystem`);
   compare each series' std to `T`.
4. **frame-id continuity:** gaps in `frame_id` (dropped frames).
5. **epoch sanity:** does `chunk_ns` look like ns since the PTP epoch (a ~1.6–1.8e18 magnitude
   if your grandmaster serves Unix/TAI time)? Is `camera_ns` the same scale?

## Decision table — what the results imply for the implementation

| Observation | Implies | Action |
|---|---|---|
| `GevIEEE1588Status` never `Slave` | PTP not working (domain/NIC/grandmaster) | Fix PTP first. Driver meanwhile runs on the `camera`/`system` fallback — timestamps carry latency/jitter. |
| `TickFrequency = 1e9` | chunk raw ticks already == ns | No conversion needed (the tick-aware code is a no-op). ✅ expected case. |
| `TickFrequency ≠ 1e9` (e.g. 125e6) | chunk is raw ticks, **not** ns; clock may be free-running, not PTP | `_ticks_to_ns()` already converts (`raw·1e9/freq`). But double-check the camera is *actually* PTP-locked — a non-1GHz freq often means it isn't. |
| `d_cl ≈ 0` (small, stable) | leader and chunk are the same clock + latch | Either works. Keep `chunk` primary, `camera` fallback. Simplest world. |
| `d_cl ≈ constant > 0` (chunk earlier) | same clock, different latch (chunk = end-of-exposure, leader = transfer start) | Keep `chunk` primary (true capture instant). Optionally record the constant as the exposure+readout offset. |
| `d_cl` varies a lot, or `camera_ns` is 0/garbage | leader timestamp unreliable on this firmware | `chunk` primary is essential; treat `system` (not `camera`) as the practical fallback. Consider dropping `camera` from the ladder. |
| chunk read fails / `has_chunks()` false | chunk mode or parser issue on this firmware | Confirm Aravis ≥ 0.8.32; verify the exact chunk node names with `arv-tool-0.8 features` (could be `ChunkFrameCounter` etc.); update `chunk_*_name` in config. Until fixed, run on `camera`. |
| `d_sc` small + stable (tens of µs – few ms) | host well-synced; arrival latency low and predictable | Confirms the PTP value is worth it; `system` is an acceptable fallback when PTP drops. |
| `d_sc` large or drifting | host not on the same PTP domain (or camera unlocked) | Fix host `phc2sys`/domain. Don't trust `system` for absolute time. The size of this gap **is** the jitter we avoid by using `chunk`. |
| `std(Δchunk) ≪ std(Δsystem)` | the camera timestamp is the clean signal (expected) | Validates using the hardware timestamp over arrival time — record the jitter-reduction number; it's the justification for this whole design. |
| `frame_id` gaps | dropped frames (network/encoder) | Tune jumbo MTU / `net.core.rmem_max` / `GevSCPD`; the CSV gap lets post-processing account for missing frames. |

## Expected outcome (hypothesis)

On a PTP-locked Blackfly S we expect: `Status=Slave`, `TickFrequency=1e9`, `chunk_ns`
and `camera_ns` equal or a small constant apart, `Δchunk` std ≪ frame period, and a
`system_ns − chunk_ns` gap of order ~0.1–a few ms with visible jitter. That would
confirm the current implementation (chunk primary, camera fallback, tick-aware) is
correct as-is, and quantify the latency/jitter we remove. Anything else routes to the
table above.
