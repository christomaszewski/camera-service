# jp6m on the vehicle — step by step

Starting point: a `rig build` for platform **jp6**, baked, shipped, and running on the JetPack 6
vehicle as `$ART` (the extracted artifact directory); the camera row is `<cam>`; the vehicle id is
`<id>` (from `/etc/rig/vehicle.local.yaml`). The modern images were built on the build host with

```bash
RIG_TARGET_PLATFORM=jp6m tools/build-images.sh <registry:port> jp6m
```

and are in the registry as `<registry:port>/cam-core:jp6m`, `webrtc-bridge:jp6m`, `ros2-bridge:jp6m`
(and `cam-dev:jp6m`, the bench image, unused here). The experiment's scripts were copied to
`~/jp6-modern` on the vehicle (`scp -r camera-service/tools/jp6-modern <vehicle>:~/jp6-modern`).
Everything below happens inside that deployment. Budget: about two hours for one camera. The long
form, with the baseline and the robustness passes, is [hardware-test-procedure.md](hardware-test-procedure.md).

Write `R=<registry:port>` once in the shell you use; the commands below say `$R`.

## 1. Prerequisites (10 min)

```bash
R=<registry:port>
docker pull $R/cam-core:jp6m && docker pull $R/webrtc-bridge:jp6m && docker pull $R/ros2-bridge:jp6m
docker images | grep ":jp6m"
nvidia-ctk --version                                # >= 1.13
sudo nvidia-ctk cdi generate --mode=csv --output=/etc/cdi/nvidia.yaml && grep -c hostPath /etc/cdi/nvidia.yaml   # for the cdi rows
cd $ART && ./rig status                             # the deployment as shipped: router, <cam>, dashboard up
```

## 2. The probe (15 min) — read this before touching the row

```bash
~/jp6-modern/probe.sh --images $R/cam-core:jp6m,$R/webrtc-bridge:jp6m     # csv + cdi
cat jp6m-results/summary.txt
```
Per cell, in order: `ldd` unresolved libraries (`ok`), plugin load reasons (no `blacklist` /
`undefined symbol`), elements (`nvvidconv`, `nvv4l2h264enc`, `nvv4l2h265enc`, `nvv4l2decoder`,
`unixfdsink` all `yes`), the two NVENC throughput lines hardware-fast against the `x264enc` baseline,
`NVENC LOSSLESS PASS`, and `webrtcsink 0.15.3` with `nvv4l2h264enc` in its rankable encoders.
The control row — the same probe against the core image the artifact pinned:
```bash
~/jp6-modern/probe.sh --images $(docker images --format '{{.Repository}}:{{.Tag}}' | grep cam-core | grep -v jp6m | head -1)
```

Decision: csv green → step 3 as written; only cdi green → step 3 with the `CAM_PLATFORM: jp7` line
from step 6 included from the start; neither green → stop, keep `jp6m-results/`, and read the Triage
section of [jp6-modern-userspace.md](jp6-modern-userspace.md).

## 3. Switch the row to the modern images (csv mode)

Append to the per-host file — it merges over the baked `vehicle.yaml` key by key, so the identity
lines it already has stay:
```bash
sudo tee -a /etc/rig/vehicle.local.yaml <<YAML
env:
  CAM_TRANSPORT: unixfd
  CAM_CORE_IMAGE: $R/cam-core:jp6m
  CAM_WEBRTC_IMAGE: $R/webrtc-bridge:jp6m
  CAM_ROS2_IMAGE: $R/ros2-bridge:jp6m
YAML
tail -6 /etc/rig/vehicle.local.yaml               # the refs expanded, not "$R"
```
Then through rig, not `run.sh` (its compose-only scripts carry the digests pinned at bake):
```bash
cd $ART
./rig down <cam>
./rig pull <cam>                                    # cam-up honours the per-image variables: fetches the three refs
./rig new-run jp6m-csv
./rig up <cam>
./rig status                                        # <cam>: running, healthy within ~60 s
docker ps --format '{{.Names}}\t{{.Image}}' | grep "^<cam>-vehicle-<id>"   # the three containers on :jp6m
```

## 4. Verdicts from the running row (10 min)

```bash
~/jp6-modern/stack-check.sh $ART/config/sensors/<cam>.yaml --project <cam>-vehicle-<id>
./rig logs <cam> | grep -iE "chunk|1588|ptp|timestamp source|encoder=|transport endpoint|health:" | head -20
```
Want, in that output:
- core `healthy`, `gst-inspect-1.0 version 1.28.x`
- `recorder: encoder=hw-hevc-lossless` (Mono8 / Bayer8; `ffv1` is right for a 16-bit camera)
- `plugin transport endpoint (unixfd)` — the 1.28 core; no ERROR lines
- `chunk mode enabled`, `GevIEEE1588Status=Slave`, `Active timestamp source = ptp_chunk` — the same as the
  baseline row gave (the code is the same; only the userspace changed)
- `health: frames=N, no drops` climbing at the frame rate
- webrtc-bridge: `webrtcsink 0.15.3`, inventory with `nvv4l2h264enc=rank …`. If it reads `ABSENT`, the
  L4T stack is not reaching that container (the bridge gets the nvidia runtime in the JP6 shape —
  check `docker inspect <cam>-vehicle-<id>-webrtc-bridge-1 --format '{{.HostConfig.Runtime}}'`). To
  make it the encoder actually used, add `GST_PLUGIN_FEATURE_RANK: nvv4l2h264enc:MAX` under the
  webrtc-bridge `params:` in `$ART/config/sensors/<cam>.yaml` and `./rig down <cam> && ./rig up <cam>`.
- ros2-bridge: `CamUnixfdBridge` loaded, and
  `docker exec <cam>-vehicle-<id>-ros2-bridge-1 bash -c "source /opt/ros/lyrical/setup.bash; ros2 topic hz --no-daemon /<cam>/image_raw"`
  at the camera's rate.

## 5. A recording, bit-exact, and the operator's view (20 min)

```bash
C=<cam>-vehicle-<id>-core-driver-1
docker kill -s USR1 $C; sleep 60; docker kill -s USR2 $C        # or the dashboard tile's ● / ● again
docker logs $C 2>&1 | grep -E "session .* (open|finalized)|sidecar" | tail -4
./rig runs                                                       # the open run holds recordings/<cam>/
```
Take the directory from the `finalized` line, then:
```bash
d=<dir>; f=$(ls -t $d/*-00000.mkv | head -1)
docker exec $C gst-discoverer-1.0 "$f" | grep -E "Duration|video|Width|Height"
head -3 $d/*.csv                                                 # chunk_ns / camera_ns / system_ns per frame
docker run --rm --runtime nvidia --network host $R/cam-core:jp6m tools/nvenc_lossless_test.py --frames 60   # NVENC LOSSLESS PASS
```
Dashboard: the camera plays; the tile's pill follows the session; the webrtc bridge's
`lat[cap->enc]` p50/p95 in `docker logs <cam>-vehicle-<id>-webrtc-bridge-1`. Numbers for the table:
```bash
docker stats --no-stream | grep "<cam>-vehicle"
tegrastats --interval 1000 | head -3
./rig end-run
```

## 6. The cdi variant (20 min)

Same swap plus the platform hint (cam-up honours `CAM_PLATFORM` over the provisioned platform and
applies the runc + CDI overlay to the core and the webrtc bridge). Add one line under `env:` in
`/etc/rig/vehicle.local.yaml`:
```yaml
  CAM_PLATFORM: jp7
```
then
```bash
cd $ART && ./rig down <cam> && ./rig new-run jp6m-cdi && ./rig up <cam>
docker inspect <cam>-vehicle-<id>-core-driver-1 --format 'runtime={{.HostConfig.Runtime}} cdi={{.HostConfig.DeviceRequests}}'
```
Repeat step 4, and the bit-exact line with `--device nvidia.com/gpu=all` instead of `--runtime nvidia`;
`./rig end-run`.

## 7. Back to the baseline, and what to keep

Remove the appended `env:` block from `/etc/rig/vehicle.local.yaml` (leave the identity keys), then:
```bash
cd $ART && ./rig down <cam> && ./rig up <cam>                    # or ./run.sh up
./rig status
```
Keep: `jp6m-results/` (written where you ran the probe), the `stack-check.sh` output per mode,
`./rig logs <cam>` per mode, one sidecar `.csv` + `.json` per run, the bit-exact output per mode,
`docker stats` and a `tegrastats` line per mode, `./rig runs`, and the dashboard's latency line.
The results table and the decision rule are at the end of
[hardware-test-procedure.md](hardware-test-procedure.md).
