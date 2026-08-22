# Fleet image build + publish — the rig contract (proposal)

How the fleet's images get from N stack repos into a registry that `rig` deploys from. This is a
**cross-repo proposal** drafted from cam (one stack); it's for `bringup` (rig) + the `boilerplate`
template to implement, and for the other stacks (the nav drivers) to back-fill. cam already implements
its side.

## Model: build once on an arm64 host, pull many

```
arm64 build host (has the stack repos)                 each vehicle (Orin)
  rig build ──► for each stack: <build.command> ──push──►  registry  ──pull──►  rig up ──► cam-up/... deploy
                (RIG_IMAGE_REGISTRY set, per platform)      (RIG_IMAGE_REGISTRY)
```

- **One registry var, two phases.** rig reads `images.registry` from `vehicle.yaml` and exports
  **`RIG_IMAGE_REGISTRY`** for BOTH the build phase (where the build script pushes to it) and the deploy
  phase (where the launcher pulls from it). No second variable.
- **Native arm64 build.** Jetsons are arm64; cam compiles Aravis + colcon, so cross-building from x86
  under qemu is slow. rig builds on an arm64 host (an Orin is fine) declared by `build.arch`.
- **Build only what's deployed.** rig iterates the stacks in the vehicle/fleet deployment (its
  `services.yaml` ∩ the vehicle config), not every repo.

## Per-stack contract — `rigging.yaml: build:`

Each stack declares how to build + publish its images:

```yaml
build:
  command: tools/build-images.sh   # builds + pushes; reads $RIG_IMAGE_REGISTRY (or arg $1); tag = arg $2
  arch: arm64                      # build host arch (native; no qemu)
  platforms: [jp7, jp6]            # platform tags this stack publishes; [default] / omit for single-arch stacks
  images: [cam-core, ros2-bridge, webrtc-bridge]   # produced as <registry>/<image>:<platform>
```

Contract for `build.command`:
- Reads the target registry from **`$RIG_IMAGE_REGISTRY`** (or positional `$1`).
- Takes the tag as `$2` and pushes `<registry>/<image>:<tag>` **verbatim** for every image in `images`.
  Two tag shapes: a bare **platform tag** (`jp7` — legacy) or a composed **`<version>-<platform>`**
  (`v1.4.0-jp7` — the versioned matrix; rig invokes once per platform in `platforms`). The jp6/jp7
  build variant comes from **`$RIG_TARGET_PLATFORM`** when rig sets it, else is derived from the tag
  (bare value or `-jp6`/`-jp7` suffix). `PUSH=0` builds without pushing (CI/dry-run).
- Exits non-zero on failure; safe to re-run (idempotent push).

Single-platform stacks (the nav drivers) set `platforms: [default]` (or omit) and push `…/<image>:latest`.

## One base image per deployment (rig ≥ v0.2.21)

Version skew is what this solves. Two images that `apt-get install ros-<distro>-rmw-zenoh-cpp`
independently, at different times, against a moving ROS repo end up with two different zenoh builds —
and their sessions cannot talk to each other. The fix is structural: **one apt-level ROS layer per
deployment**, shared by every ROS container on the vehicle.

rig resolves the deployment's base from vehicle.yaml `images.base` (a full ref, used verbatim) or from a
service declaring `build: {…, provides: base}` (rig-infra's `fleet-ros`), builds it **first** (stage 0,
aborting the run if it fails), and exports it to every build command:

| env var | set when | this stack does |
|---|---|---|
| `RIG_BASE_IMAGE` | a base is resolved (e.g. `devbox:5000/fleet-ros:v1.3.0`) | passes it **verbatim** as ros2-bridge's `BASE_IMAGE` build-arg |
| `RIG_BUILD_NO_CACHE` | `rig build --no-cache` | adds `--no-cache` to **every** image it builds |

Both are optional, and both use the `${VAR:+…}` form in
[`tools/build-images.sh`](../tools/build-images.sh): absent or empty means the flag is not passed at
all, so the Dockerfile's own default applies and a plain `docker build` (and this repo's CI, which runs
`rig certify` with `RIG_BASE_IMAGE` deliberately unset) keeps working untouched.

**The base ref is a CONSUMER input — do not compose it.** rig already did any composition on the
provider side. In particular, do *not* append `-$CAM_PLATFORM` to it: this stack's `build.platforms`
matrix governs the tags it *publishes* (`…/ros2-bridge:v1.3.0-jp7`), not the base it *consumes*
(`…/fleet-ros:v1.3.0`). If a per-platform base ever becomes genuinely necessary — a CUDA/L4T fleet base,
where jp6 and jp7 need different bases — that is a change to the rig contract (a per-platform
`images.base`), not something to invent here by string-munging the ref.

**Which images rebase.** Only `ros2-bridge`. It is the only image in this repo carrying a ROS/RMW apt
stack, so it is the only one that can drift from the fleet's other ROS containers:

| image | base | `/opt/ros` | rebased? |
|---|---|---|---|
| `ros2-bridge` | `ros:<distro>-ros-base` → `${BASE_IMAGE}` | yes | **yes** |
| `cam-core` | `nvcr.io/nvidia/l4t-base` (jp6) / `ubuntu:24.04` (jp7) | no | no — needs an L4T/NVENC base, carries no ROS |
| `webrtc-bridge` | `ubuntu:24.04` | no | no — GStreamer/Rust only; its zenoh is the *Python* `eclipse-zenoh` wheel, not an RMW |
| `rtsp-bridge` | `ubuntu:24.04` | no | no — same, GStreamer only |
| `ros1-bridge` | `ros:noetic-ros-base` | yes (ROS 1) | no — ROS 1; a ROS 2 base is not a valid `FROM` |

**Rebasing is only half of it — `apt-get install --no-upgrade`.** A `FROM` change alone does *not*
eliminate skew. Plain `apt-get install <pkg>` on a package the base already carries silently **upgrades**
it to the repo's current version, so the image drifts off the shared base within one layer. This was
measured, not theorized: after rebasing, `rig image audit` still reported
`version skew: ros-lyrical-rclcpp-components` (…`20260731` in `fleet-ros` vs …`20260812` in the freshly
built bridge). `--no-upgrade` on both stages fixes it — base-pinned packages stay pinned, packages the
base lacks still install (so the rig-less build on stock `ros-base` stays complete). Any image rebased
onto a fleet base needs the same flag.

**Detection and remediation.** `rig image audit` inspects every image the deployment's stacks resolve
to and cross-checks distro, the declared RMW package, and `ros-*` versions *across* images; drift shows
as `version skew: ros-<distro>-<pkg>`. `rig build --no-cache` re-converges a fleet that already
drifted.

## rig build loop (pseudocode for `bringup`)

```python
reg = vehicle.images.registry                      # e.g. devbox:5000
for stack in deployed_stacks:                       # services.yaml ∩ vehicle config
    d = load(stack.repo / "rigging.yaml")           # falls back to the legacy deploy.yaml
    if not d.build: continue                        # stack with no build entrypoint (CI-published) -> skip
    tags = d.build.platforms or ["default"]
    tags = [t for t in tags if t in fleet_target_platforms]   # build only what's needed
    for tag in tags:
        run([d.build.command, "", tag],             # registry via env (one source of truth)
            cwd=stack.repo, env={**base_env, "RIG_IMAGE_REGISTRY": reg},
            host=arm64_builder(d.build.arch))
# then, per vehicle:  rig up  ->  RIG_IMAGE_REGISTRY=reg <launcher> <config> up -d   (pulls from reg)
```

## The platform-tag gotcha (why cam needed `cam-up` wiring)

The compose prefix alone — `${RIG_IMAGE_REGISTRY}/cam-core` — resolves to `:latest`, but cam publishes
per-platform tags. So cam's launcher maps `RIG_IMAGE_REGISTRY` into its registry logic and composes the
pull ref via the per-image override that wins in compose: with a **version-valued** `images.tag`
(`v1.4.0`) it appends the resolved platform (`…/cam-core:v1.4.0-jp7`, matching what `build.command`
pushed); a legacy **platform-valued** tag (`jp7`) or a bare-platform standalone deploy passes through as
`…/cam-core:jp7`. The platform itself resolves `CAM_PLATFORM` > `RIG_TARGET_PLATFORM` (vehicle.yaml
`platform:`) > legacy platform-valued tag > host detection. Single-`:latest` stacks don't hit this; any
multi-platform stack must do the same.

## Prereqs / open items
- **Registry reachable from both** the arm64 build host and every vehicle (the `docker-registry` repo;
  insecure-registry trust or TLS).
- **Auth**, if the registry is private (build host pushes, vehicles pull).
- **Nav drivers** publish a CI image today + a manual runtime `docker build/push`; to be `rig build`-able
  uniformly they need a `build.command` like cam's (best put in the `boilerplate` template).
