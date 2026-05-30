# Plugins (consumer applications)

Plugins are **separate containers** that consume the video stream the core driver
publishes, decoupled from the core. A plugin never talks to the camera — it attaches
to the shared transport and does its own thing (republish, encode, analyze, forward).

## The contract

A plugin consumes:
1. **Transport** — the core publishes frames on a shared endpoint (a Unix-socket /
   shared-memory transport on the same host). Phase plan:
   - **JP6 (now):** `shmsink` (CPU shared memory) or `unixfdsink` (DMABUF fd passing).
   - **JP7 / Thor (later):** `nvunixfdsink` for true zero-copy GPU (`NvBufSurface`) sharing.
   Because the publish branch is just `tee. ! queue ! <sink>`, swapping transports is a
   localized change, not a rearchitecture.
2. **Caps** — the negotiated raw video format (e.g. `GRAY8`, width/height, framerate).
3. **Time** — the per-frame **PTP capture time travels in the buffer PTS**. A consumer
   reads the PTS and (with `base_timestamp_ns` from the recording's `.json` sidecar)
   recovers absolute time. Example: ros2-bridge sets `sensor_msgs/Image.header.stamp`
   directly from the PTS — no separate metadata channel needed.

## Planned plugins

- **ros2-bridge** — reads frames from the transport, publishes `sensor_msgs/Image` (and
  `CameraInfo`) with `header.stamp` derived from the PTP PTS. First plugin of interest.
- **webrtc** — re-encodes lossy/low-latency (HEVC/H.264) and serves remote viewers via
  `webrtcsink`. A consumer like any other (its own encode, allowed to drop frames).
- **mqtt-telemetry** — grabs select low-res frames and forwards to the cloud (example).

## Writing a plugin

Minimum: a container that opens the transport source element, matches the caps, and reads
buffers (via `appsink` or a pad probe). Keep it best-effort (leaky queues) — the core's
recording branch must never be stalled by a slow consumer.
