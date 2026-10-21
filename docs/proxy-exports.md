# Offline replay proxy exports

Camera-service owns the default `h264` proxy profile in
[`export-profiles/h264.yaml`](../export-profiles/h264.yaml). Rig selects and stores
exports; camera-service performs and validates the conversion. No encoding options
or export profile definitions are required in `vehicle.yaml`.

```sh
rig run export RUN --service camera-service
rig run export RUN --service camera-service --profile h264 --dry-run
rig run export RUN --service camera-service --instance old_camera_name
rig replay check RUN --use-proxies
rig replay RUN --use-proxies
```

The original recordings are always the default. Replay and preparation use proxies
only with `--use-proxies`, and never generate missing proxies implicitly. A missing,
changed or incompatible requested proxy fails with an actionable error. These
exports are lossy: use originals when pixel fidelity matters.

Rig stores only transformed camera data beneath the source run:

```text
RUN/exports/services/camera-service/h264/<export-id>/
  export.yaml
  recordings/<recorded-instance>/<session>.json
  recordings/<recorded-instance>/<session>.csv
  recordings/<recorded-instance>/<session>-00000.mkv
```

Bags and unchanged services stay at the original run paths. Export identities and
input hashes allow reuse after moving or sharing the run, independent of deployment
instance names. `--force` creates another generation without replacing old ones.

## The h264 profile

Revision 1 uses software x264, CRF 23, `veryfast`, constrained baseline, no CABAC or
B-frames, one reference frame, approximately one-second keyframe spacing, and
60-second MKV segments. It preserves every frame, resolution, frame ID, capture
timestamp, session boundary and recorded gap. Existing capture metadata and CSV
fields remain intact. Storage metadata is regenerated and the result is decoded
again to verify frame count and correspondence with the sidecar timeline.

Eight-bit mono, Bayer and supported RGB/YUV formats are accepted. Lossless CFA
storage tiling is undone before conversion; replay still delivers the Bayer mosaic
and its original pattern, with approximate pixels. Storage is I420, so color data
also incurs chroma subsampling. No scaling or frame-rate reduction occurs.

High-bit-depth/thermal data, alpha-bearing RGBA/BGRA, unknown formats, invalid/odd
dimensions, incomplete segments and invalid sidecars are rejected before conversion.
There is no fallback to FFV1 masquerading as an h264 proxy. Limit export selection by
recorded instance name and use a recipe `proxies.instances.<name>.use: original`
preference for unsupported replay instances.

The profile targets lower software decode cost; it does not bypass the current raw-frame
transport or WebRTC re-encoding. It does not enable Apple VideoToolbox/NVIDIA codecs.
CPU/size benefits depend on the recording; benchmark representative data before
choosing proxies for a workload.

## Runtime and helper contract

The bundled executable `tools/export-recordings` accepts `--request FILE --result
FILE`, plus `--preflight` for read-only file and local-image inspection. Requests and
results are JSON, version 1. Rig supplies the original run root, an empty staging
destination, the canonical profile definition/hash, and exact recorded names and
session selections. Empty selection lists in a request mean discover all sessions.
The result lists portable relative files, geometry, frame counts, capture time
ranges, validation results, and producer identity. Rig computes the final artifact
inventory and commits the validated staging directory atomically.

The helper uses only the Python standard library on the host. It runs the converter
from a local native ARM64/AMD64 `cam-dev` image with inputs mounted read-only, an
isolated writable staging directory, and networking disabled. It does not invoke
`cam-up`, Compose, ROS, Zenoh, camera discovery or shared socket volumes. `CAM_DEV_IMAGE`
pins an image; otherwise the existing registry/tag environment selects the `dev`
variant. The image must be explicitly built or pulled first, and must contain the
same profile definition. Old images fail rather than running an outdated converter.
Replay also requires an updated camera image. Both camera runtime builds declare
`io.rig.camera.replay-bindings=1`; Rig inspects the selected image before reporting
replay ready or starting it. An old image lacking that capability fails with an
upgrade error, so it cannot silently ignore the exact `replay.sessions` selection.
Update the installed service bundle and its runtime images together.

Profile definitions are JSON-compatible YAML so inspection needs no host YAML
package. Canonical identity is SHA-256 of Python's
`json.dumps(definition, sort_keys=True, separators=(',', ':'), ensure_ascii=True)`.
Changing encoding semantics requires a profile revision. The profile and converter
ship in the native image; the helper, profile and pure preflight dependencies ship
in the service launch bundle.

## Direct replay bindings and provenance

`replay.sessions` is an optional nonempty list of exact session prefixes, mutually
exclusive with `replay.run`. It requires a directory input; all requested sessions
must exist, and they play in capture-time order. Rig binds an individual original
or proxy directory through `replay_input_path` and an exact selection through
`replay_input_sessions`. This supports renamed instances and subsets without copying
bag data into a synthesized run tree.

Each proxy header contains a `proxy` record with profile identity, lossiness,
transformations and original relative file hashes. Playback diagnostics expose it,
and re-recorded replay sidecars preserve it in `replay_proxies`, including Rig's
resolved export identity when supplied through `replay.proxy`. Original recordings
that happened to use x264 are not classified as derived proxies.

## Validation

Run `python3 core-driver/tests/test_proxy_export.py` for host-only contract tests.
Inside a native dev image, run `core-driver/tests/test_proxy_export_gst.py` for real
FFV1-to-H264, Bayer, incomplete-input and replay stamp tests. Both are collected by
the existing CI matrix. `core-driver/tools/replay_buffer_benchmark.py` compares
original/proxy CPU with recording disabled and an attached transport consumer; use
the same image, playback window, pacing and frame count for both inputs.

Initial local measurements (2026-10-21) used a synthetic 300-frame 1280×720 GRAY8
SMPTE sequence at 30 fps, FFV1 with four slices, native ARM64 Docker on a Mac, and
GStreamer 1.28.8. Recording was off and a UnixFD transport consumer received every
frame. The original and proxy runs used the same ten-second window:

| Concurrent camera processes | Original process CPU | Proxy process CPU | Delivered frames |
| --- | --- | --- | --- |
| 1 | 5.996 s | 4.113 s | 300 / 300 for both |
| 3 | 10.770 s total | 11.213 s total | 900 / 900 for both |

Neither case reported missing frames, publish drops or enqueue failures. Media size
fell from 20,324,831 to 2,683,634 bytes. The single-camera sample used about 31% less
CPU; the three-camera sample used about 4% more. A trial with two H.264 decoder
threads did not improve the latter, so no decoder-thread change was retained.

These results establish conversion correctness and smaller files for this fixture,
not a general replay CPU improvement. Representative camera footage, the full
WebRTC path and native AMD64 performance were not measured. The profile remains an
explicit, lossy choice; benchmark the intended workload before relying on CPU gains.
