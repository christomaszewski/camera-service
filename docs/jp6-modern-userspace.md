# JP6 with a modern userspace — test plan (branch `jp6-modern`)

> The vehicle-side steps for just this experiment, inside a baked rig deployment, are
> [jp6m-vehicle-runbook.md](jp6m-vehicle-runbook.md).
>
> The day's full procedure — preflight, today's baseline with the real camera, this experiment,
> robustness, the rig shape, the write-up — is [hardware-test-procedure.md](hardware-test-procedure.md).

**Question.** Can a JetPack 6 host (L4T r36, Ubuntu 22.04, GStreamer 1.20) run the camera stack in
**Ubuntu 26.04 containers** — GStreamer **1.28**, gst-plugins-rs **0.15** `webrtcsink` — with the
host's NVIDIA multimedia stack (`nvvidconv`, `nvv4l2h264enc`, `nvv4l2h265enc`, `nvv4l2decoder`,
NvBufSurface) injected into them, and with **hardware encoding actually working**?

**Why it matters.** Today the JP6 build pins the core to `l4t-base:r36` = GStreamer 1.20: no `unixfd`
transport, a 2022 `webrtcsink`, a different userspace from JP7 (24.04 / 1.24). If this works, one
modern userspace serves both JetPack generations — the fleet gets `unixfd`, the newest congestion
control / codec support in `webrtcsink`, and one image matrix instead of two.

**Hypothesis.** The NVIDIA GStreamer plugins are ordinary plugins built against the 1.20 API; the
plugin ABI is stable across 1.x, their NVIDIA libraries are mounted alongside them, and a newer glibc
runs older binaries. JP7 already proves the mechanism with matching versions (CDI-injected 1.24
plugins into a 1.24 container). The unknowns are the version skew (1.20-built plugins in 1.28), the
JP6 CSV mount list (does it carry everything a 1.28 container needs, e.g. the patched `libv4l2`), and
the library path (a plain Ubuntu base does not list `/usr/lib/aarch64-linux-gnu/tegra` for the
linker — the `jp6m` images do).

## What was built (on the branch)

| Piece | Change |
|---|---|
| `core-driver/Dockerfile` | `BASE_IMAGE=ubuntu:26.04` builds; the tegra dirs are on the linker path (`/etc/ld.so.conf.d/nvidia-tegra.conf` + `LD_LIBRARY_PATH`) so mounted `libgstnv*.so` resolve `libnvbufsurface` & co. |
| `plugins/webrtc-bridge/Dockerfile` | `BASE_IMAGE` + `GST_RS_TAG` build args (24.04 / 0.13.7 default; 26.04 / 0.15.3 for jp6m); same tegra linker path |
| `plugins/webrtc-bridge/compose.yml` | the two build args are wired (`CAM_WEBRTC_BASE`, `CAM_GST_RS_TAG`) |
| `tools/build-images.sh` | a `jp6m` variant (`RIG_TARGET_PLATFORM=jp6m`, or a `jp6m` / `*-jp6m` tag): 26.04 base, gst-plugins-rs 0.15.3, images cam-core + ros2-bridge + webrtc-bridge |
| `tools/jp6-modern/probe.sh` | the on-host probe: injection mode × image → plugin load, elements, encode throughput, decode round trip, bit-exact NVENC |
| `tools/jp6-modern/cam-up-jp6m` | `cam-up` with the jp6m images in `csv`, `cdi` or `dev` mode |
| `core-driver/Dockerfile.dev` | unchanged — it already builds on `BASE=ubuntu:26.04` (26.04 packages Aravis 0.8.34), giving `cam-dev:jp6m` for the bench row |
| `tools/jp6-modern/stack-check.sh` | reads a running stack's logs for the verdicts (encoder, transport, webrtcsink's encoder, ros2 consumer) |

ros2-bridge is untouched: it is already the Lyrical (26.04, GStreamer 1.28) image, and the new
`ros2-source` writer rides in it.

## Validated on the bench before the Jetson (arm64 Mac, no NVIDIA stack)

- `cam-core:jp6m` builds on `ubuntu:26.04`: GStreamer 1.28.2, Python 3.14, the Aravis 0.8.34 binding
  and eclipse-zenoh import, `unixfdsink` / `unixfdsrc` / `aravissrc` present. The core's
  GStreamer-backed test files all pass inside it (shm input incl. the unixfd framing, pipeline PTS /
  reconnect / session, recorder, replay, playback, config) — the core code is fine on 1.28 and 3.14.
- `webrtc-bridge:jp6m` builds on `ubuntu:26.04` with gst-plugins-rs 0.15.3 (`webrtcsink`,
  `rtpgccbwe`, the signalling server).
- `probe.sh` and `stack-check.sh` were exercised here in no-injection mode (mode `none` against the
  24.04 core image; the checker against the bench's running stack): the mechanics work, the NVIDIA
  rows read `no` as they must without a Jetson.
- The 26.04 package set differs from 24.04 in one runtime name (`libxml2` → `libxml2-16`); the core
  Dockerfile picks whichever the base has.

**The `dev` row ran here, on the bench deployment (a Docker Desktop VM, three instances: a pcap
playback, a shm-tap fed by a 1.20 `shmsink`, and a ROS 2 topic fed instance), all on the jp6m
images:** every core healthy on 1.28 and publishing `unixfd`; `webrtc-bridge` 0.15.3 consuming it
with native caps and the dashboard playing all three feeds; `ros2-bridge`'s `CamUnixfdBridge`
publishing; `ros2-source` → `unixfdsink` → the core's `unixfdsrc` end to end (the framing that needs
1.24+ on both sides); the recorder on FFV1. So the userspace, the transport and the whole plugin
chain are proven on 26.04 — what the Jetson adds is the injected NVIDIA stack.

What is NOT known until the Jetson: everything in the matrix — the injected plugins loading into
1.28, HW encode, the recorder's bit-exact path, `webrtcsink` picking `nvv4l2h264enc`.

## Prerequisites on the Jetson (JP6)

```bash
cat /etc/nv_tegra_release                 # R36.x
docker --version; nvidia-ctk --version    # nvidia-container-toolkit >= 1.13 for csv-mode CDI
ls /etc/nvidia-container-runtime/host-files-for-container.d/   # the CSV mount lists (l4t.csv, ...)
# cdi mode (the JP7 shape on JP6) -- once, re-run after a JetPack update:
sudo nvidia-ctk cdi generate --mode=csv --output=/etc/cdi/nvidia.yaml
grep -c hostPath /etc/cdi/nvidia.yaml     # a few hundred mounts
```

Get the images there. Either pull the fleet registry after a bench build:

```bash
# bench (arm64), from the repo root on branch jp6-modern:
RIG_TARGET_PLATFORM=jp6m tools/build-images.sh <registry:port> jp6m
# Jetson:
export CAM_REGISTRY=<registry:port>
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml pull
```

or load the tarballs a bench build saved (`docker save cam-core:jp6m webrtc-bridge:jp6m
ros2-bridge:jp6m | gzip > jp6m-images.tgz`; on the Jetson `docker load < jp6m-images.tgz`). The
baseline `cam-core:jp6` (l4t-base, GStreamer 1.20) is what the vehicle runs today — keep it around for
the A/B rows.

## Test matrix

Two injection modes × three images on the Jetson (plus the `dev` row, already green on the bench —
`JP6M_MODE=dev tools/jp6-modern/cam-up-jp6m …`). Every cell is one probe run; the interesting cells
are bold.

| | `cam-core:jp6` (22.04 / 1.20, baseline) | **`cam-core:jp6m`** (26.04 / 1.28) | **`webrtc-bridge:jp6m`** (26.04 / 0.15 webrtcsink) |
|---|---|---|---|
| **csv** — `--runtime nvidia` (JP6 default) | must pass: proves the host + the probe | **the question** | HW encode inside webrtcsink? |
| **cdi** — `--device nvidia.com/gpu=all` from a csv-mode spec | should pass | same question, JP7-shaped | same |

## Steps

### 1. Sanity on the host (5 min)

```bash
gst-inspect-1.0 nvv4l2h264enc | head -3      # the host's own stack works
docker run --rm --runtime nvidia cam-core:jp6 gst-inspect-1.0 --version   # ENTRYPOINT is python3: add --entrypoint gst-inspect-1.0
```

### 2. The probe (15 min)

```bash
tools/jp6-modern/probe.sh                      # csv,cdi x the three images -> ./jp6m-results/
cat jp6m-results/summary.txt
```

Read, per cell, in this order:

1. **Unresolved libraries** (`ldd` per `libgstnv*.so`): `ok` means the mounted stack is complete for
   this userspace. A `not found` names the missing library: if it is an NVIDIA one, the CSV list lacks
   it (add a line to `l4t.csv` or use cdi mode, which carries the full generated list); if it is a
   system library (`libv4l2`, `libgst*`), the 26.04 package set differs — note the soname.
2. **Plugin load reasons**: `blacklist` / `undefined symbol` here is the ABI-skew verdict. An undefined
   `gst_*` symbol means a 1.20-built plugin references something 1.28 removed (unlikely); an undefined
   `Nv*` symbol means item 1.
3. **Elements**: `nvvidconv`, `nvv4l2h264enc`, `nvv4l2h265enc`, `nvv4l2decoder` all `yes`, plus
   `unixfdsink` (the 1.28 core will publish it) and `aravissrc`.
4. **Throughput**: the two NVENC lines should be hardware-fast (hundreds of fps at 1080p H.264; the
   2048x1536 lossless HEVC line is the recorder's own path) and far above the `x264enc` software
   baseline; a FAILED line with `not negotiated` points at NVMM caps / `nvvidconv`.
5. **Bit-exact NVENC** (core images): `NVENC LOSSLESS PASS` — the recorder's correctness gate, not
   just "it ran".
6. **webrtcsink** (bridge image): version 0.15.x, and `nvv4l2h264enc` in the encoders it can rank.

Decision after step 2: if `cam-core:jp6m` × csv is green on items 1–5, continue with csv (zero host
setup). If only cdi is green, continue with cdi and note why csv fell short. If neither loads the
plugins, see Triage before spending time on the stack.

### 3. The real sensor, modern stack (20 min)

```bash
# csv mode (default); the wrapper points the images at :jp6m, the base at 26.04, and tells the
# bridges the core publishes unixfd (a jp6 platform label would make them expect the header endpoint)
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml up -d
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml ps          # core healthy
tools/jp6-modern/stack-check.sh config/sensors/<cam>.yaml
```

`stack-check.sh` wants: core `healthy`; `recorder: encoder=hw-hevc-lossless`; `plugin transport
endpoint (unixfd)`; a frames health line climbing; no errors; webrtc-bridge on GStreamer 1.28 with
webrtcsink 0.15 and `nvv4l2h264enc` in its inventory (set `GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX`
in the sensor yaml's webrtc-bridge params to make it the pick); ros2-bridge with `CamUnixfdBridge`.

Then the operator's view: activate a recording from the dashboard (or `rig`), let it run a minute,
deactivate; the `.mkv` under `/data/recordings` must decode (`gst-discoverer-1.0 <file>` inside the
core container) and the sidecar CSV must show the camera's timestamp source. Open the dashboard
(webrtc-bridge on `:8443`), watch the stream for a few minutes, and read the bridge's status lines
(latency percentiles, congestion control engaging).

Repeat once in cdi mode:

```bash
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml down
JP6M_MODE=cdi tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml up -d
tools/jp6-modern/stack-check.sh config/sensors/<cam>.yaml
```

### 4. A/B against today's stack (10 min)

Bring the same sensor up the normal way (`./cam-up config/sensors/<cam>.yaml up -d`, the jp6 images)
and note the same numbers: recorder encoder, core CPU (`docker stats`), webrtc latency percentiles,
frames dropped. This is the row that says whether the modern userspace costs anything.

### 5. Write-up

Attach `jp6m-results/` and the `stack-check.sh` output for each mode, and fill in:

| | csv | cdi |
|---|---|---|
| nv plugins load in 1.28 | | |
| `nvv4l2h264enc` 1080p fps | | |
| `nvv4l2h265enc` lossless fps | | |
| bit-exact NVENC | | |
| core: encoder chosen | | |
| core: transport | | |
| webrtcsink version / encoder picked | | |
| ros2-bridge consumer | | |
| recording decodes | | |
| dashboard stream OK (min) | | |
| anything the baseline (`jp6`) does better | | |

## Triage

- **`libgstnv*.so` fails to load, `not found` on an NVIDIA library** → the mount list. Compare
  `ls /usr/lib/aarch64-linux-gnu/tegra` inside the container with the host's; in csv mode add the
  missing file to `/etc/nvidia-container-runtime/host-files-for-container.d/l4t.csv`; cdi mode's
  generated spec is usually more complete.
- **`not found` on `libv4l2` / a `t64` soname** → NVIDIA's patched `libv4l2.so.0.0.999999` is CSV-mounted
  over the container's; if the 26.04 chain differs, the `sym` lines in the CSV don't land. Check
  `ls -l /usr/lib/aarch64-linux-gnu/libv4l2*` inside the container; a manual `-v` bind of the host file
  is a fine experiment.
- **plugin loads, element exists, encode `not negotiated`** → NVMM caps. Try `nvvidconv` alone
  (`videotestsrc ! nvvidconv ! 'video/x-raw(memory:NVMM)' ! fakesink`) and `GST_DEBUG=3`.
- **encoder starts then aborts** → `kmod` (libnvtvmr runs `lsmod`; baked in) or a missing
  `/dev/nvhost-msenc` / `/dev/v4l2-nvenc` (the probe lists devices).
- **everything loads but the numbers match x264** → the encoder fell back to software; check
  `GST_PLUGIN_FEATURE_RANK` and the bridge's inventory line.
- **26.04 is the problem, not the mechanism** (odd `t64` sonames, a Python 3.14 binding) → the same
  scripts run the proven JP7 combination: build with `BASE_IMAGE=ubuntu:24.04` / `GST_RS_TAG=0.13.7`
  (`BASE_IMAGE=ubuntu:24.04 WEBRTC_BASE=ubuntu:24.04 GST_RS_TAG=0.13.7 RIG_TARGET_PLATFORM=jp6m
  tools/build-images.sh …`), which still lifts JP6 to GStreamer 1.24 + unixfd.
- **only cdi works** → productize on cdi: one `nvidia-ctk cdi generate --mode=csv` per host, then the
  JP7 overlay as-is.

## Under a rig deployment (a baked artifact on the vehicle)

The wrapper is for a bare checkout. Inside a rig deployment the same switch is four `env:` lines in
the per-host `/etc/rig/vehicle.local.yaml` (merged over the baked `vehicle.yaml`'s env), the jp6m
images `docker load`ed on the vehicle, and `./rig up <cam>` — not `./run.sh up`, whose compose-only
scripts carry the digests pinned at bake time:

```yaml
env:
  CAM_TRANSPORT: unixfd          # a 1.28 core publishes unixfd; the jp6 platform label would say header
  CAM_CORE_IMAGE: cam-core:jp6m
  CAM_WEBRTC_IMAGE: webrtc-bridge:jp6m
  CAM_ROS2_IMAGE: ros2-bridge:jp6m
  # CAM_PLATFORM: jp7            # cdi mode: the runc + CDI overlay (after nvidia-ctk cdi generate --mode=csv)
```

This is what the bench deployment runs (`playback/viewer/vehicle.yaml`). The step-by-step is
[hardware-test-procedure.md](hardware-test-procedure.md).

## If it is green: what productizing looks like

A real `jp6m` platform in `cam-up` (`--jp6m`; auto-detect stays jp6 — a host has to ask for it),
in `rigging.yaml` `platform` / `build.platforms`, and in the bridges' transport selection (`unixfd`
for jp7 **or** jp6m). `tools/build-images.sh` already knows the variant. Until then everything here
runs through the wrapper and the existing `--jp6` / `--jp7` shapes.
