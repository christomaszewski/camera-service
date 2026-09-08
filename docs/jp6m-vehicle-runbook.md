# jp6m on the vehicle — step by step (the platform way)

Where things stand (2026-09-08): the probe is green on uav6 (R36.4.4) — the 26.04 / GStreamer 1.28
containers hardware-encode, bit-exact — and `jp6m` is a platform (`a1fd924`+): rig builds, pins and
runs the set like any other; no per-host `env:` swap. This is the day's remaining sequence. The
registry is `<registry:port>` (`R=10.160.1.69:5000` on this bench), the camera row `<cam>`, the vehicle
id `<id>`, the extracted artifact `$ART`. Budget: ~2 h for one camera.

## 1. Build host: pull, declare the platform, build, bake, ship (30–50 min, mostly the webrtc Rust build once)

```bash
cd camera-service && git pull                          # branch jp6-modern, a1fd924 or later; the checkout services.yaml routes to
```
Declare the vehicle's platform as `jp6m` wherever the deployment declares it for the build (the same
place that produced today's jp6 build — the deployment's `vehicle.local.yaml`, or the fleet roster),
then from the deployment directory:
```bash
rig build                                              # the -jp6m set: cam-core, ros2-bridge, webrtc-bridge -> <registry>/<image>:<tag>-jp6m
rig bake --tag <t>                                     # pins those digests
scp var/artifacts/<t>.tar.gz <vehicle>:~/
```
(`rig build` builds every platform the fleet declares; a fleet with jp6 AND jp6m vehicles gets both sets.)

## 2. Vehicle: provision, extract, up (10 min)

```bash
sudo rig provision --platform jp6m                     # -> /etc/rig/vehicle.local.yaml; cam-up reads it at up
grep -v '^#' /etc/rig/vehicle.local.yaml               # no leftover env: block from the swap route
tar xzf ~/<t>.tar.gz && cd <t> && ./run.sh up          # or ./rig up
./rig status                                           # router, <cam>, dashboard: running / healthy
docker ps --format '{{.Names}}\t{{.Image}}' | grep "^<cam>-vehicle-<id>"   # the three containers on ...:<t>-jp6m
```

## 3. Optional: the plain probe on the pinned images (5 min)

Already answered by the host-libs run; this just confirms the pinned images ask the runtime themselves:
```bash
~/jp6-modern/probe.sh --modes csv --images $(docker ps --format '{{.Image}}' | grep cam-core | head -1),$(docker ps --format '{{.Image}}' | grep webrtc-bridge | head -1)
cat jp6m-results/summary.txt                           # nvvidconv yes, NVENC fps, NVENC LOSSLESS PASS -- no --hostlibs
```

## 4. Verdicts from the running row (10 min)

```bash
~/jp6-modern/stack-check.sh $ART/config/sensors/<cam>.yaml --project <cam>-vehicle-<id>
./rig logs <cam> | grep -iE "chunk|1588|ptp|timestamp source|encoder=|transport endpoint|health:" | head -20
```
Want: core `healthy` on `1.28.x`; `recorder: encoder=hw-hevc-lossless` (Mono8 / Bayer8; `ffv1` for
16-bit); `plugin transport endpoint (unixfd)`; no ERROR lines; `chunk mode enabled`,
`GevIEEE1588Status=Slave`, `Active timestamp source = ptp_chunk` — the same lines the jp6 baseline gave;
`health: frames=N, no drops` climbing; webrtc-bridge `webrtcsink 0.15.3` with `nvv4l2h264enc=rank …`
(`GST_PLUGIN_FEATURE_RANK: nvv4l2h264enc:MAX` in its params makes it the pick); ros2-bridge
`CamUnixfdBridge` and `ros2 topic hz --no-daemon /<cam>/image_raw` at the camera's rate.

## 5. A recording, bit-exact, and the operator's view (20 min)

```bash
./rig new-run jp6m
C=<cam>-vehicle-<id>-core-driver-1
docker kill -s USR1 $C; sleep 60; docker kill -s USR2 $C        # or the dashboard tile's ● / ● again
docker logs $C 2>&1 | grep -E "session .* (open|finalized)|sidecar" | tail -4
./rig runs
```
From the `finalized` line's directory: the mkv decodes (`docker exec $C gst-discoverer-1.0 <mkv>`), the
sidecar CSV carries chunk / camera / system stamps, and the bit-exact gate on the pinned image:
```bash
docker run --rm --runtime nvidia --network host $(docker ps --format '{{.Image}}' | grep cam-core | head -1) tools/nvenc_lossless_test.py --frames 60
```
Dashboard: the feed plays; the tile's pill follows the session; the bridge's `lat[cap->enc]` p50/p95.
Numbers: `docker stats --no-stream | grep "<cam>-vehicle"`, `tegrastats --interval 1000 | head -3`; then
`./rig end-run`.

Watch one number: the probe's lossless-HEVC line ran at 15 fps for a 3 MP frame (the GRAY8 → NV24
conversion before NVENC is software). Compare against your camera's resolution × rate; the jp6 baseline
runs the identical path, so it is a property of the recorder on this Orin, not of 1.28.

## 6. A/B against the jp6 baseline (15 min)

Keep the jp6 artifact extracted beside this one. `./rig down` here, `sudo rig provision --platform jp6`,
`./run.sh up` in the jp6 artifact, `./rig new-run baseline`, and take the same lines and numbers: encoder,
transport (`shm+header`), timestamp source, frames / drops, topic Hz, latency percentiles, CPU. Then back:
`./rig down`, `--platform jp6m`, `./run.sh up` here.

## 7. Robustness (30 min, optional today)

Camera cable out 20 s and back (reopen lines, session survives), `docker restart $C` (bridges
reconnect alone), a bridge restart (tile / topic recover), `./rig down <cam> && ./rig up <cam>` under an
open run, a reboot. Record recovery times.

## 8. Optional: the cdi shape (20 min)

Same images, the JP7 runtime shape on this host: `sudo nvidia-ctk cdi generate --mode=csv
--output=/etc/cdi/nvidia.yaml` once, then in `/etc/rig/vehicle.local.yaml` an `env:` with
`CAM_PLATFORM: jp7` (the runc + CDI overlay) and `CAM_IMAGE_TAG: <t>-jp6m` (keeps the pinned tag; the
jp7 label would otherwise ask for `-jp7` images), `./rig down <cam> && ./rig up <cam>`, step 4 again,
the bit-exact line with `--device nvidia.com/gpu=all`. Remove the `env:` afterwards.

## 9. Keep

`jp6m-results/`, the `stack-check.sh` output and `./rig logs <cam>` per configuration, one sidecar
`.csv` + `.json` per run, the bit-exact outputs, `docker stats` + a `tegrastats` line per
configuration, `./rig runs`, the dashboard's latency line, and `metadata.yaml` of each artifact. The
results table and the decision rule are at the end of
[hardware-test-procedure.md](hardware-test-procedure.md).

---

## Appendix — the env-swap route (an artifact baked before the branch)

Images from `1d7faa8`+ pulled or loaded on the vehicle, and in `/etc/rig/vehicle.local.yaml`:
```yaml
env:
  CAM_TRANSPORT: unixfd
  CAM_CORE_IMAGE: <registry:port>/cam-core:jp6m
  CAM_WEBRTC_IMAGE: <registry:port>/webrtc-bridge:jp6m
  CAM_ROS2_IMAGE: <registry:port>/ros2-bridge:jp6m
```
then `./rig down <cam> && ./rig pull <cam> && ./rig up <cam>` (through `./rig`, not `run.sh`, whose
compose-only scripts carry the baked digests). Steps 4–9 as above. Only for an artifact whose vendored
`cam-up` predates `docker-compose.jp6.yml`; its images must set `NVIDIA_VISIBLE_DEVICES` themselves,
which `1d7faa8`+ do.
