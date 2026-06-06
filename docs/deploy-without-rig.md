# Deploying camera-service to a Jetson via a local registry (no rig, no source on the vehicle)

Build images once on a build host → push to your local registry. Package the **launch surface** (cam-up
+ compose fragments + sensor_env) → scp to the vehicle. The vehicle **pulls** images and runs cam-up from
that small bundle — no source tree, no Dockerfiles, no build on the vehicle. (rig will later automate the
vehicle side; the bundle is exactly what it vendors.)

Images are **platform-pinned by tag** (`jp7`/`jp6`); `cam-up` auto-detects the host and pulls the matching
tag, so the deploy commands are identical on every vehicle of a given JetPack.

```
build host (arm64, has source)                      vehicle (no source)
  build-images.sh ───────push images──►  registry  ──pull──►  cam-up <cfg> pull && up -d
  package-launch-surface.sh ─────────────scp bundle──────────►  ~/cam  (cam-up + compose only)
```

---

## 0. One-time — a local registry
If you don't already run one, on a host reachable from the vehicles:
```bash
docker run -d -p 5000:5000 --restart=always --name registry registry:2     # call it e.g. registry.lan:5000
```
If it's plain HTTP (no TLS), every Docker daemon that builds or pulls from it must trust it (step 2a).

## 1. Build host (arm64, has the repo) — build images + package the launch surface
```bash
git clone https://github.com/christomaszewski/camera-service && cd camera-service
./tools/build-images.sh registry.lan:5000        # build + push cam-core/ros2-bridge/webrtc-bridge :jp7
./tools/package-launch-surface.sh                # -> dist/camera-launch.tar.gz (cam-up + compose only)
```
- Skip the slow webrtc build: `IMAGES="cam-core ros2-bridge" ./tools/build-images.sh registry.lan:5000`
- Cross-build from x86: `PLATFORM_FLAG=--platform=linux/arm64 ./tools/build-images.sh …` (needs qemu binfmt; slow)
- JP6 images: `./tools/build-images.sh registry.lan:5000 jp6` — the `jp6` tag auto-selects the **slim
  `l4t-base:r36.2.0`** (not the ~14 GB `l4t-jetpack`; we use no CUDA SDK). Match the `r36.x` to the vehicle's
  L4T (r36.4.0 = JP6.2) via `BASE_IMAGE=…` if needed, and `docker login nvcr.io` first if the base pull is
  denied. Only `cam-core` differs by platform (l4t/GStreamer-1.20 base for CSV-injected NVENC); the plugins
  are platform-agnostic, so you can re-tag instead of rebuilding them:
  `for p in ros2-bridge webrtc-bridge; do docker tag registry.lan:5000/$p:jp7 registry.lan:5000/$p:jp6 && docker push registry.lan:5000/$p:jp6; done`

## 2. New vehicle — one-time prep (no source tree)
```bash
# trust the registry IF it's HTTP/insecure (skip if it has TLS)
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{ "insecure-registries": ["registry.lan:5000"] }
JSON
sudo systemctl restart docker

# host deps: the docker compose v2 plugin. (PyYAML is OPTIONAL — sensor_env.py falls back to a stdlib
# parser, so stock python3 is enough; add python3-yaml only if you want the full YAML parser.)
sudo apt-get update && sudo apt-get install -y docker-compose-plugin
```
**Start the shared zenoh router (once per host).** The stack defaults to `rmw_zenoh_cpp`, which discovers
through one `rmw_zenohd` router per host. `rig` runs it in production; standalone, bring up exactly one
(idempotent) — it reuses the ros2-bridge image you already pull:
```bash
CAM_REGISTRY=registry.lan:5000 CAM_ROS2_IMAGE=registry.lan:5000/ros2-bridge:jp7 ~/cam/tools/zenohd.sh up
#   ... or let cam-up do it for you below with `--zenohd`.
```
**Make NVENC reachable in containers — this is the one step that differs by JetPack** (cam-up applies the
matching compose wiring automatically once it detects the platform):
- **JP7 → CDI:** `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (re-run after a JetPack update).
- **JP6 → nvidia runtime + CSV, _no_ CDI:** make sure `nvidia-container-toolkit` is installed (ships with
  JP6) and the nvidia runtime is registered — `docker info | grep -i runtimes` should list `nvidia`; if not,
  `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker`. (cam-up detects
  `jp6` → skips the jp7 overlay → the base compose's `runtime: nvidia` does the CSV injection.)
```bash
# the launch-surface bundle from step 1 — scp it over, extract (the ONLY cam code on the vehicle)
#   from your workstation:  scp <build-host>:camera-service/dist/camera-launch.tar.gz <vehicle>:~
mkdir -p ~/cam && tar -xzf ~/camera-launch.tar.gz -C ~/cam && cd ~/cam
```
If `docker` needs sudo: `sudo usermod -aG docker $USER && newgrp docker`.

## 3. Deploy a sensor — supply a config, pull, run
The bundle ships **no configs** (they're per-deployment). Provide your sensor config as a standalone file;
cam-up bind-mounts it in (external-config path), so it can live anywhere.
```bash
cd ~/cam
export CAM_REGISTRY=registry.lan:5000        # cam-up points every image at the registry, tag = detected platform

# a fake-camera smoke: copy the demo config over once (from the build host):
#   scp <build-host>:camera-service/core-driver/config/sensors/cam_a.yaml <vehicle>:~/cam_a.yaml
# a real camera: same file with  camera.fake: false  +  camera.camera_id: <serial>

./cam-up ~/cam_a.yaml pull                   # REQUIRED: fetch images (the bundle has no build context to fall back on)
./cam-up --zenohd ~/cam_a.yaml up -d         # --zenohd also ensures the shared host router is up (drop it if rig runs it)
./cam-up ~/cam_a.yaml ps                      # health -> 'healthy' once the transport socket is up
```
The status line should read `platform=jp7 registry=registry.lan:5000 … rmw=rmw_zenoh_cpp config=/run/cam-sensor.yaml (external: …)`.
Multiple cameras = repeat with each config file; each becomes its own project, socket volume, ROS ns, and ports.

## 4. Verify
```bash
./cam-up ~/cam_a.yaml logs core-driver       # "pipeline: running", transport endpoint (unixfd) on JP7 / (shm+header) on JP6
./cam-up ~/cam_a.yaml logs ros2-bridge       # "consuming … -> publishing /<name>/image_raw"  (name from the config)

# ROS 2 — an independent node on the same zenoh graph (image already pulled as a layer). --ipc=host is only
# needed to read the JP6 shm transport; harmless on JP7. Under rmw_zenoh add --no-daemon to topic echo/hz
# (the daemon-backed CLI often shows nothing even though data flows; typed subscriber nodes are unaffected).
docker run --rm -it --network host --ipc=host -e ROS_DOMAIN_ID=0 -e RMW_IMPLEMENTATION=rmw_zenoh_cpp \
  registry.lan:5000/ros2-bridge:jp7 ros2 topic list
#   ... ros2 topic echo --no-daemon /<name>/image_raw --field encoding   # bayer_rggb8 (or rgb8 with debayer)
```
NVENC HW-lossless recording is off in `cam_a.yaml`; enable `recording.enabled: true` + `encoder: auto` to
exercise it.

## 5. Update / teardown
```bash
# update: rebuild+push on the build host, then on each vehicle:
./cam-up ~/cam_a.yaml pull && ./cam-up ~/cam_a.yaml up -d
# new launcher/compose (rare): re-run package-launch-surface.sh, re-scp, re-extract over ~/cam
./cam-up ~/cam_a.yaml down                    # teardown
```

---

## Notes & knobs
- **`pull` is mandatory here.** With only the bundle, there's no build context on the vehicle, so a bare
  `up` would try (and fail) to build. `pull` fetches the registry images; `up` then just runs them.
- **One var, whole stack:** `CAM_REGISTRY` → `<registry>/<name>:<platform>` for all three images. Override
  one with `CAM_CORE_IMAGE`/`CAM_ROS2_IMAGE`/`CAM_WEBRTC_IMAGE`, or pin a release with `CAM_IMAGE_TAG`.
- **The bundle can't drift:** `package-launch-surface.sh` reads the file list straight from `rigging.yaml`'s
  `launch_surface`, the same contract rig uses.
- **No CUDA base needed:** NVENC reaches the container via CDI at runtime, so `cam-core` is a plain
  arm64/24.04 image — identical across every JP7 Orin.
- **Air-gapped vehicle (no registry):** `docker save <reg>/cam-core:jp7 ros2-bridge:jp7 | gzip > imgs.tgz`,
  scp alongside the launch bundle, `gunzip -c imgs.tgz | docker load`, set the per-image vars, skip `pull`.
