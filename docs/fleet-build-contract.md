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
- Takes the **platform tag** as `$2` (one of `platforms`); builds + pushes `<registry>/<image>:<tag>` for
  every image in `images`. `PUSH=0` builds without pushing (CI/dry-run).
- Exits non-zero on failure; safe to re-run (idempotent push).

Single-platform stacks (the nav drivers) set `platforms: [default]` (or omit) and push `…/<image>:latest`.

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
`cam-core:jp7` / `:jp6`. So cam's launcher maps `RIG_IMAGE_REGISTRY` into its registry logic, which
appends the **detected platform tag** (`…/cam-core:jp7`) via the per-image override that wins in compose.
Single-`:latest` stacks don't hit this; any multi-platform stack must do the same.

## Prereqs / open items
- **Registry reachable from both** the arm64 build host and every vehicle (the `docker-registry` repo;
  insecure-registry trust or TLS).
- **Auth**, if the registry is private (build host pushes, vehicles pull).
- **Nav drivers** publish a CI image today + a manual runtime `docker build/push`; to be `rig build`-able
  uniformly they need a `build.command` like cam's (best put in the `boilerplate` template).
