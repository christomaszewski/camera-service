# Fleet image build contract

Rig builds all declared runtime variants and CPU architectures by default. The deployment's
`platform` still selects what runs; use `rig build --platform dev --arch amd64 --tag bench-1`
for a targeted iteration. Use the same tag in the deployment when launching that iteration.

| Runtime | Architectures | Images |
| --- | --- | --- |
| jp6 | arm64 | cam-core, ros2-bridge, ros1-bridge, webrtc-bridge |
| jp6m, jp7 | arm64 | cam-core, ros2-bridge, webrtc-bridge |
| dev | arm64, amd64 | cam-dev, ros2-bridge, webrtc-bridge |

Runtime tags are distinct (`v1-jp6m`, `v1-dev`); multiarch indexes choose a CPU within a runtime.
The dev core and WebRTC bridge use pinned GStreamer on Ubuntu 26.04 with software codecs, runc,
and unixfd transport. There is no additional amd64-specific runtime name.

`tools/build-images.sh REGISTRY TAG` takes its runtime from `RIG_TARGET_PLATFORM`, otherwise from
the tag suffix. Rig supplies `RIG_BUILD_PLATFORMS` (comma-separated Docker platforms) and
`RIG_BUILD_RESULT` (JSONL output path for published digests). The shared helper in `tools/rig-build.sh`
uses Buildx publication and provenance for these builds. `RIG_BUILD_NO_CACHE=1` forwards
`--no-cache --pull` to every image build. Standalone calls without `RIG_BUILD_PLATFORMS` retain
host-native Docker builds and the existing `PLATFORM_FLAG` override.

```sh
# Standalone native dev build; full default image set, no publication.
PUSH=0 RIG_TARGET_PLATFORM=dev tools/build-images.sh registry.lan:5000 bench-dev
# Standalone publication of both CPU members.
RIG_BUILD_PLATFORMS=linux/arm64,linux/amd64 RIG_TARGET_PLATFORM=dev tools/build-images.sh registry.lan:5000 v1-dev
# No image build or registry access: command-contract certification.
rig certify --repo . --require-build-check
```

A Buildx builder must advertise the requested architectures and be able to reach the registry.
Native builder nodes avoid expensive emulation. `PUSH=0` under Buildx permits a single CPU member;
multiarch output must be pushed. `IMAGES` remains a standalone subset override; portable release
builds must produce the complete declared set or Rig rejects their result report.

Rig carries the full companion image catalog into baked artifacts and run captures. Reconstruction
chooses the replay host's provisioned runtime, otherwise this descriptor's `replay.platform: dev`.
Use `rig reconstruct RUN --platform recorded` for deliberate Jetson replay. Missing companion images
or CPU members fail before replay starts infrastructure.

Dev launch surfaces run the code in the image. Live source editing is explicit:
`cam-up --dev --source CONFIG up -d`. The source option requires a full checkout containing
`core-driver/supervisor.py`; a vendored launch surface cannot hide the image's `/app`.

Existing artifacts need updated descriptors/launchers and rebuilt images. An uncached build does not
advance Rig's pinned source revision. The isolated certification check needs Bubblewrap (Linux) or
sandbox-exec (macOS); CI requires it to complete. Native CI uses both CPU architectures, but actual
Jetson capture and software replay of Jetson-produced recordings still require hardware acceptance.
