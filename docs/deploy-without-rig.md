# Deploying gige-vision to a Jetson via a local registry (no rig, no source on the vehicle)

Build images once on a build host → push to your local registry. Package the **launch surface** (gige-up
+ compose fragments + sensor_env) → scp to the vehicle. The vehicle **pulls** images and runs gige-up from
that small bundle — no source tree, no Dockerfiles, no build on the vehicle. (rig will later automate the
vehicle side; the bundle is exactly what it vendors.)

Images are **platform-pinned by tag** (`jp7`/`jp6`); `gige-up` auto-detects the host and pulls the matching
tag, so the deploy commands are identical on every vehicle of a given JetPack.

```
build host (arm64, has source)                      vehicle (no source)
  build-images.sh ───────push images──►  registry  ──pull──►  gige-up <cfg> pull && up -d
  package-launch-surface.sh ─────────────scp bundle──────────►  ~/gige  (gige-up + compose only)
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
git clone https://github.com/christomaszewski/gige-vision-service && cd gige-vision-service
./tools/build-images.sh registry.lan:5000        # build + push gige-core/ros2-bridge/webrtc-bridge :jp7
./tools/package-launch-surface.sh                # -> dist/gige-vision-launch.tar.gz (gige-up + compose only)
```
- Skip the slow webrtc build: `IMAGES="gige-core ros2-bridge" ./tools/build-images.sh registry.lan:5000`
- Cross-build from x86: `PLATFORM_FLAG=--platform=linux/arm64 ./tools/build-images.sh …` (needs qemu binfmt; slow)
- JP6 images: `BASE_IMAGE=nvcr.io/nvidia/l4t-jetpack:r36.4.0 ./tools/build-images.sh registry.lan:5000 jp6`

## 2. New vehicle — one-time prep (no source tree)
```bash
# a) trust the registry IF it's HTTP/insecure (skip if it has TLS)
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{ "insecure-registries": ["registry.lan:5000"] }
JSON
sudo systemctl restart docker

# b) host deps gige-up shells out to: docker compose v2 plugin + PyYAML (sensor_env.py runs on the host)
sudo apt-get update && sudo apt-get install -y docker-compose-plugin python3-yaml

# c) NVENC via CDI (JP7) — once; re-run after a JetPack update
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

# d) the launch-surface bundle from step 1 — scp it over and extract (this is the ONLY gige code on the vehicle)
#    from your workstation:  scp <build-host>:gige-vision-service/dist/gige-vision-launch.tar.gz <vehicle>:~
mkdir -p ~/gige && tar -xzf ~/gige-vision-launch.tar.gz -C ~/gige && cd ~/gige
```
If `docker` needs sudo: `sudo usermod -aG docker $USER && newgrp docker`.

## 3. Deploy a sensor — supply a config, pull, run
The bundle ships **no configs** (they're per-deployment). Provide your sensor config as a standalone file;
gige-up bind-mounts it in (external-config path), so it can live anywhere.
```bash
cd ~/gige
export GIGE_REGISTRY=registry.lan:5000        # gige-up points every image at the registry, tag = detected platform

# a fake-camera smoke: copy the demo config over once (from the build host):
#   scp <build-host>:gige-vision-service/core-driver/config/sensors/cam_a.yaml <vehicle>:~/cam_a.yaml
# a real camera: same file with  camera.fake: false  +  camera.camera_id: <serial>

./gige-up ~/cam_a.yaml pull                   # REQUIRED: fetch images (the bundle has no build context to fall back on)
./gige-up ~/cam_a.yaml up -d
./gige-up ~/cam_a.yaml ps                      # health -> 'healthy' once the shm socket is up
```
The status line should read `platform=jp7 registry=registry.lan:5000 … config=/run/gige-sensor.yaml (external: …)`.
Multiple cameras = repeat with each config file; each becomes its own project, shm volume, ROS ns, and ports.

## 4. Verify
```bash
./gige-up ~/cam_a.yaml logs core-driver       # "pipeline: running", transport endpoint -> /tmp/gige/frames
./gige-up ~/cam_a.yaml logs ros2-bridge       # publishing /<name>/image_raw   (name comes from the config)

# ROS 2 — an independent node on the same DDS graph (image already pulled as a layer):
docker run --rm -it --network host --ipc=host -e ROS_DOMAIN_ID=0 ros:lyrical-ros-base ros2 topic list
```
NVENC HW-lossless recording is off in `cam_a.yaml`; enable `recording.enabled: true` + `encoder: auto` to
exercise it.

## 5. Update / teardown
```bash
# update: rebuild+push on the build host, then on each vehicle:
./gige-up ~/cam_a.yaml pull && ./gige-up ~/cam_a.yaml up -d
# new launcher/compose (rare): re-run package-launch-surface.sh, re-scp, re-extract over ~/gige
./gige-up ~/cam_a.yaml down                    # teardown
```

---

## Notes & knobs
- **`pull` is mandatory here.** With only the bundle, there's no build context on the vehicle, so a bare
  `up` would try (and fail) to build. `pull` fetches the registry images; `up` then just runs them.
- **One var, whole stack:** `GIGE_REGISTRY` → `<registry>/<name>:<platform>` for all three images. Override
  one with `GIGE_CORE_IMAGE`/`GIGE_ROS2_IMAGE`/`GIGE_WEBRTC_IMAGE`, or pin a release with `GIGE_IMAGE_TAG`.
- **The bundle can't drift:** `package-launch-surface.sh` reads the file list straight from `deploy.yaml`'s
  `launch_surface`, the same contract rig uses.
- **No CUDA base needed:** NVENC reaches the container via CDI at runtime, so `gige-core` is a plain
  arm64/24.04 image — identical across every JP7 Orin.
- **Air-gapped vehicle (no registry):** `docker save <reg>/gige-core:jp7 ros2-bridge:jp7 | gzip > imgs.tgz`,
  scp alongside the launch bundle, `gunzip -c imgs.tgz | docker load`, set the per-image vars, skip `pull`.
```
