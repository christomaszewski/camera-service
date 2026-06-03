# Deploying gige-vision to a Jetson via a local registry (no rig)

Build the images once on a build host, push them to your local registry, then `pull` + run on each
vehicle. No per-vehicle build, no rig. (rig will later automate steps 3–4; the contract is the same.)

The images are **platform-pinned by tag** (`jp7` / `jp6`). `gige-up` auto-detects the host platform and
pulls the matching tag, so the deploy commands are identical on every vehicle of a given JetPack.

```
build host (arm64) ──push──►  local registry  ──pull──►  vehicle Jetson
  tools/build-images.sh        registry.lan:5000          gige-up <cfg> pull && up -d
```

---

## 0. One-time — a local registry
If you don't already run one, on a host reachable from the vehicles:
```bash
docker run -d -p 5000:5000 --restart=always --name registry registry:2
```
Call it e.g. `registry.lan:5000`. If it's plain HTTP (no TLS), **every** Docker daemon that builds or
pulls from it (the build host *and* every vehicle) must trust it — see step 2a.

## 1. Build host — build + push the images
Run on an **arm64** host (an Orin is ideal — native build). Clone the repo, then:
```bash
git clone https://github.com/christomaszewski/gige-vision-service && cd gige-vision-service
./tools/build-images.sh registry.lan:5000            # builds + pushes gige-core, ros2-bridge, webrtc-bridge as :jp7
```
- Subset (e.g. skip webrtc): `IMAGES="gige-core ros2-bridge" ./tools/build-images.sh registry.lan:5000`
- Build only, no push: `PUSH=0 ./tools/build-images.sh registry.lan:5000`
- Cross-build from an x86 box: `PLATFORM_FLAG=--platform=linux/arm64 ./tools/build-images.sh registry.lan:5000` (needs `qemu` binfmt; the Aravis/webrtc source builds are slow under emulation — native arm64 is much faster)
- JP6 images: `BASE_IMAGE=nvcr.io/nvidia/l4t-jetpack:r36.4.0 ./tools/build-images.sh registry.lan:5000 jp6`

## 2. New vehicle — one-time prep
```bash
# a) trust the registry IF it's HTTP/insecure (skip if it has TLS)
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{ "insecure-registries": ["registry.lan:5000"] }
JSON
sudo systemctl restart docker

# b) NVENC via CDI (JP7) — once per vehicle; re-run after a JetPack update
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml

# c) the launch surface — until rig ships, just clone the repo (gige-up + compose + sensor_env + configs).
#    Only the files in deploy.yaml's launch_surface are actually used; the Dockerfiles go unused (we pull).
git clone https://github.com/christomaszewski/gige-vision-service && cd gige-vision-service
```
If `docker` needs sudo: `sudo usermod -aG docker $USER && newgrp docker`.

## 3. Deploy a sensor — pull, then run
```bash
export GIGE_REGISTRY=registry.lan:5000        # gige-up now points every image at the registry, tag = detected platform
# pick/author a sensor config: cam_a.yaml is the fake-camera demo; a real camera sets
#   camera.fake: false  +  camera.camera_id: <serial>  (any path works — gige-up resolves it)
./gige-up config/sensors/cam_a.yaml pull      # pulls gige-core + the enabled plugins (NOT builds)
./gige-up config/sensors/cam_a.yaml up -d
./gige-up config/sensors/cam_a.yaml ps        # health column -> 'healthy' once the shm socket is up
```
The status line should read `platform=jp7 registry=registry.lan:5000`. Multiple cameras = repeat with each
config — each becomes its own compose project, shm volume, ROS namespace, and ports.

## 4. Verify
```bash
./gige-up config/sensors/cam_a.yaml logs core-driver    # "pipeline: running", transport endpoint -> /tmp/gige/frames
./gige-up config/sensors/cam_a.yaml logs ros2-bridge    # publishing /<name>/image_raw

# ROS 2 — an independent node on the same DDS graph sees the topic:
docker run --rm -it --network host --ipc=host -e ROS_DOMAIN_ID=0 ros:lyrical-ros-base ros2 topic list
#   -> /cam_a/image_raw   (then: ros2 topic hz /cam_a/image_raw)
```
Recording (NVENC HW-lossless) is off in `cam_a.yaml`; enable `recording.enabled: true` + `encoder: auto`
in a config to exercise it, and `tools/nvenc_lossless_test.sh` proves it's bit-exact on the box.

## 5. Update / teardown
```bash
# update: rebuild+push on the build host (same or new tag), then on each vehicle:
./gige-up config/sensors/cam_a.yaml pull && ./gige-up config/sensors/cam_a.yaml up -d
# teardown:
./gige-up config/sensors/cam_a.yaml down
```

---

## Notes & knobs
- **One var, whole stack:** `GIGE_REGISTRY` sets `gige-core`, `ros2-bridge`, `webrtc-bridge` image refs to
  `<registry>/<name>:<platform>`. Override any one with `GIGE_CORE_IMAGE` / `GIGE_ROS2_IMAGE` /
  `GIGE_WEBRTC_IMAGE`, or pin a non-platform tag with `GIGE_IMAGE_TAG` (e.g. a release version).
- **Why `pull` before `up`:** the vehicle has the repo (build contexts present), so a bare `up` could
  *build* instead of pull. `pull` fetches the registry images first; `up` then just runs them.
- **No CUDA base needed:** NVENC reaches the container via CDI at runtime, so `gige-core` is a plain
  arm64/24.04 image — the same image runs on any JP7 Orin.
- **Air-gapped vehicle (no registry):** `docker save <reg>/gige-core:jp7 | gzip > x.tgz`, scp, then
  `gunzip -c x.tgz | docker load`; set `GIGE_CORE_IMAGE` to the loaded ref and skip `pull`.
