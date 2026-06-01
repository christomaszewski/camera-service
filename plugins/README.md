# Plugins (consumer applications)

Plugins consume the video stream the core publishes, decoupled from the core — a heavy
plugin runs as its own container, a lightweight one as an in-image process. A plugin never
talks to the camera; it attaches to the shared transport and does its own thing (republish,
encode, analyze, forward).

## The contract

A plugin consumes:
1. **Transport** — the core publishes frames on a shared endpoint (a Unix-socket /
   shared-memory transport on the same host). Phase plan:
   - **JP6 (now):** `shmsink` (CPU shared memory) + a 36-byte frame header (see Time below).
   - **JP7 / Thor (later):** `nvunixfdsink` for true zero-copy GPU (`NvBufSurface`) sharing.
   Because the publish branch is just `tee. ! queue ! <sink>`, swapping transports is a
   localized change, not a rearchitecture.
2. **Caps** — the negotiated raw video format (e.g. `GRAY8`, width/height, framerate).
3. **Time** — shm transmits only bytes (PTS + `GstMeta` are dropped at the process boundary),
   so the per-frame **PTP capture time + frame_id travel in the 36-byte header** prepended to
   each frame (caps `application/x-gige-frame`). The consumer parses the header and stamps its
   messages from it — e.g. ros2-bridge sets `sensor_msgs/Image.header.stamp` from the header's
   `timestamp_ns`. (An optional header-free `video/x-raw` endpoint exists for generic tools.)

## Plugins

- **[ros2-bridge](ros2-bridge)** — *built.* Publishes `sensor_msgs/Image` (raw + lazy compressed)
  with `header.stamp` derived from the PTP capture time. ROS 2 (Lyrical Luth).
- **[webrtc-bridge](webrtc-bridge)** — *built.* Re-encodes lossy/low-latency and serves remote
  viewers via `webrtcsink`. A best-effort consumer (its own encode, allowed to drop frames).
- **mqtt-telemetry** — *example / idea.* Grab select low-res frames and forward to the cloud.

Each plugin is either a lightweight in-image process (`isolation: process`, spawned by the
supervisor) or a heavy sibling container (`isolation: container`, e.g. the two above). The
per-sensor config selects which run, and `gige-up` brings up the stack — see the top-level README.

## Writing a plugin

Minimum: a container that opens the transport source element, matches the caps, and reads
buffers (via `appsink` or a pad probe). Keep it best-effort (leaky queues) — the core's
recording branch must never be stalled by a slow consumer.
