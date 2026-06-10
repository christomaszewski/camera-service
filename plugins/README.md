# Plugins (consumer applications)

Plugins consume the video stream the core publishes, decoupled from the core — a heavy
plugin runs as its own container, a lightweight one as an in-image process. A plugin never
talks to the camera; it attaches to the shared transport and does its own thing (republish,
encode, analyze, forward).

## The contract

A plugin consumes:
1. **Transport** — the core publishes frames on a shared per-sensor `plugin_endpoint`, whose
   implementation `pipeline.build()` picks at runtime by capability (see
   [docs/unixfd-migration.md](../docs/unixfd-migration.md)):
   - **JP7 (GStreamer ≥ 1.24):** `unixfdsink` at `/tmp/cam/unixfd` — native caps, PTS intact,
     `frame_id` in the buffer `offset`, absolute capture ns in `offset_end`. **Replaces** the
     header endpoint on JP7 (the core does *not* publish both).
   - **JP6 (GStreamer 1.20):** `shmsink` at `/tmp/cam/frames` + a 36-byte frame header
     (see Time below).
   - **Thor / later:** `nvunixfdsink` for true zero-copy GPU (`NvBufSurface`) sharing.
   An optional header-free **raw `video/x-raw` shm endpoint** (`/tmp/cam/raw`,
   `transport.raw_endpoint.enabled`) exists on both platforms for generic tools — that's what
   the webrtc-bridge reads. The publish branch is a localized sink swap, not a rearchitecture.
2. **Caps** — the negotiated video format. On JP7/unixfd the stream is self-describing
   (`video/x-raw` GRAY8/16, or `video/x-bayer` with the pattern for 8-bit CFA); on JP6 the
   header endpoint's caps are `application/x-cam-frame` and the geometry/format ride the header.
3. **Time** — on **JP6**, shm transmits only bytes (PTS + `GstMeta` are dropped at the process
   boundary), so the per-frame **PTP capture time + frame_id travel in the 36-byte header**
   prepended to each frame; the consumer parses it and stamps its messages — e.g. ros2-bridge
   sets `sensor_msgs/Image.header.stamp` from the header's `timestamp_ns`. On **JP7**, the same
   metadata rides the GstBuffer fields (`offset` = frame_id, `offset_end` = absolute capture ns).
   The ros2-bridge ships both consumers (`CamHeaderBridge` / `CamUnixfdBridge`), selected by
   `CAM_PLATFORM` in its launch.

## Plugins

- **[ros2-bridge](ros2-bridge)** — *built.* Publishes `sensor_msgs/Image` (raw + lazy compressed)
  with `header.stamp` derived from the PTP capture time. ROS 2 (Lyrical Luth).
- **[webrtc-bridge](webrtc-bridge)** — *built.* Re-encodes lossy/low-latency and serves remote
  viewers via `webrtcsink`. A best-effort consumer (its own encode, allowed to drop frames).
- **mqtt-telemetry** — *example / idea.* Grab select low-res frames and forward to the cloud.

Each plugin is either a lightweight in-image process (`isolation: process`, spawned by the
supervisor) or a heavy sibling container (`isolation: container`, e.g. the two above). The
per-sensor config selects which run, and `cam-up` brings up the stack — see the top-level README.

## Writing a plugin

Minimum: a container that opens the transport source element, matches the caps, and reads
buffers (via `appsink` or a pad probe). Keep it best-effort (leaky queues) — the core's
recording branch must never be stalled by a slow consumer.
