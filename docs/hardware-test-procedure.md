# camera-service on the JP6 hardware — the full test procedure (rig deployment)

One day, one JetPack 6 Orin with real sensors, the camera-service instance running **inside the rig
deployment you baked and shipped** (`rig bake` → artifact → `./run.sh up` on the vehicle). Seven
parts, in this order; each ends with what to record. A–B are today's stack under rig (the baseline
every later number is compared to), C–E are the modern-userspace experiment
([jp6-modern-userspace.md](jp6-modern-userspace.md)) run inside the same deployment, F is the
vehicle's own runs, replay and the dashboard's control widgets, G the write-up. **Stop and capture
at the first part that fails.** Time budgets are for one camera.

Conventions: `<cam>` is the instance's row name in `vehicle.yaml` (its config is
`config/sensors/<cam>.yaml` in the artifact), `<id>` the vehicle id from provisioning, `$ART` the
extracted artifact directory, `$C` the core container
(`docker ps --format '{{.Names}}' | grep "^<cam>-vehicle-<id>-core-driver"`). The bundled CLI is
`./rig` in `$ART` (an installed `rig` works the same from inside that directory). The core image's
ENTRYPOINT is `python3`, so non-python tools inside it need `--entrypoint`.

Two ways to bring a row up, and they are NOT interchangeable for this test:
- `./run.sh up` — the compose-only form: images **digest-pinned at bake time**, never a build. The
  production path; use it for the baseline.
- `./rig up <cam>` — the rig-runnable tree through the vendored `cam-up`: image names still come from
  the `CAM_*_IMAGE` variables, so the per-host `env:` block can swap in the jp6m images. Use it for
  parts D–E.

---

## A. Preflight (30 min, before touching the camera)

### A1. Host facts
```bash
cat /etc/nv_tegra_release                      # R36.x = JetPack 6
uname -r; docker --version
docker info | grep -i runtimes                 # nvidia + runc
nvidia-ctk --version                           # >= 1.13 (needed for csv-mode CDI in part D)
ls /etc/nvidia-container-runtime/host-files-for-container.d/    # l4t.csv & friends: the CSV mount lists
gst-inspect-1.0 --version                      # host GStreamer 1.20.x
gst-inspect-1.0 nvv4l2h265enc | head -2        # the host's own NVENC works
cat /etc/rig/vehicle.local.yaml                # vehicle, vehicle_id, platform: jp6, data_dir (from provision)
df -h <data_dir>                               # room for recordings (lossless 5 MP mono ~ 1-2 GB/min)
```
Record: JetPack/L4T version, toolkit version, vehicle id, data_dir, free disk.

### A2. Camera network + PTP (GigE only)
```bash
sudo ip link set <iface> mtu 9000
sudo sysctl -w net.core.rmem_max=33554432
sudo ptp4l -i <iface> -m &        # the host is the PTP grandmaster the camera slaves to
sudo phc2sys -a -r &
ping -c 2 <camera-ip>
```
Find the camera and its vendor's chunk/PTP feature names with the deployment's own core image
(`docker images | grep cam-core` shows the pinned one):
```bash
docker run --rm --network host --entrypoint arv-tool-0.8 <cam-core image>                 # "<Vendor>-<serial>"
docker run --rm --network host --entrypoint arv-tool-0.8 <cam-core image> features | grep -iE "chunk|1588|ptp"
```
Record: camera id string, PixelFormat, whether ChunkTimestamp / ChunkFrameID / GevIEEE1588 exist.

### A3. The deployment, and the experiment's scripts and images
The artifact is already on the vehicle. Confirm it is the one you think, and healthy on paper:
```bash
cd $ART
cat metadata.yaml | head                       # tag, parent, bake time
./rig doctor                                   # read-only preflight: configs, images, ports, platform
./rig config-render <cam>                      # the sensor config as the core will read it (name filled in)
./rig status
```
The experiment's scripts are not part of a launch surface, so put the camera-service checkout beside
the artifact (the branch is small; it is only used for `tools/jp6-modern/*` and `sensor_env.py`):
```bash
git clone -b jp6-modern https://github.com/christomaszewski/camera-service ~/camera-service
```
Load the modern image set (built on the bench; the baseline images are already pinned in the artifact):
```bash
docker load < jp6m-images.tar
docker images | grep -E "cam-core|cam-dev|ros2-bridge|webrtc-bridge"      # :jp6m beside the pinned digests
```

### A4. The sensor config
The config is the artifact's `config/sensors/<cam>.yaml`. If it needs a change for the day (the id
from A2, `gige.pixel_format`, chunk names, `recording.encoder: auto`, the webrtc port), edit it in
place — a field edit is legitimate, and `./rig bake` inside the extracted artifact later records the
lineage — then check it:
```bash
./rig config-render <cam> | head -60
./rig doctor
```

### A5. Infra rows
`./rig status` must show the zenoh-router (and the dashboard, if it is a row) up. If not:
```bash
./run.sh up            # brings every enabled row up in order (infra -> sensors -> autonomy)
```

---

## B. Baseline — today's stack under rig with the real camera (45 min)

### B1. Up, and the timestamp spine
```bash
./rig new-run baseline                          # a labelled run: recordings land in <data_dir>/runs/<stamp>_baseline/
./rig up <cam>                                  # or ./run.sh up for the whole deployment
./rig status                                    # the row: running / healthy (up to ~60 s)
./rig logs <cam> | grep -iE "chunk|1588|ptp|timestamp source|encoder=|transport endpoint|health:" | head -20
```
Expect: `chunk mode enabled`, `GevIEEE1588Status=Slave` (within `ptp_lock_timeout_s`),
`Active timestamp source = ptp_chunk`, `recorder: encoder=hw-hevc-lossless` (Mono8/Bayer8) or `ffv1`
(16-bit), `plugin transport endpoint (shm+header)` (the 1.20 core), `health: frames=N, no drops`
climbing at the frame rate.

If PTP never locks: the [PTP experiment](ptp-timestamp-experiment.md) is exactly this camera's
question — run it now (it only needs the sidecar CSV from B2) and note the provenance the core fell
back to (`camera` or `system`).

### B2. A recording, and is it lossless
camera-service declares no `standby`/`activate` verbs to rig (`./rig activate` skips it with a note);
its recording lifecycle is the zenoh control plane the dashboard drives, and the core also takes
signals. Either:
- dashboard: the camera tile's ● (`activate <cam>`) → pill `active`; ● again → `inactive`; or
- `docker kill -s USR1 $C; sleep 60; docker kill -s USR2 $C` (the supervisor forwards both to the core).
```bash
docker logs $C 2>&1 | grep -E "session .* (open|finalized)|sidecar" | tail -4
./rig runs                                      # the open run, with the instance's recordings under it
```
The session's dir is in the `finalized` line (`<data_dir>/runs/<stamp>_baseline/recordings/<cam>/<prefix>-<stamp>-*.mkv`
+ `.csv` + `.json`). Then:
```bash
d=<that dir>; f=$(ls -t $d/*-00000.mkv | head -1)
docker exec $C gst-discoverer-1.0 "$f" | grep -E "Duration|video|Width|Height"
head -3 $d/<prefix>-<stamp>.csv                              # chunk_ns / camera_ns / system_ns per frame
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print({k:d[k] for k in ('encoder','ptp_synced','frames','timestamp_source') if k in d})" $d/<prefix>-<stamp>.json
```
Lossless proof, on the exact recorder fragment, with the deployment's pinned core image and the JP6
CSV mounts:
```bash
docker run --rm --runtime nvidia --network host <cam-core image> tools/nvenc_lossless_test.py --frames 60
```
Expect `NVENC LOSSLESS PASS`, `60/60 frames bit-exact`. Record: encoder, MB per minute, PASS.
(README "Post-processing" has the decode-and-compare recipe for a real frame.)

### B3. The bridges and the dashboard
```bash
docker exec <cam>-vehicle-<id>-ros2-bridge-1 bash -c \
  "source /opt/ros/lyrical/setup.bash; ros2 topic hz --no-daemon /<cam>/image_raw"     # the camera's fps
docker logs <cam>-vehicle-<id>-webrtc-bridge-1 2>&1 | grep -E "inventory|status:" | tail -2
python3 ~/camera-service/plugins/webrtc-bridge/tools/discovery_probe.py --connect tcp/localhost:7447   # EVENT PUT fleet/<id>/media/<cam> + DESCRIPTOR_OK
```
Open the dashboard row's page (`http://<orin-ip>:<web_port>`, 8080 by default): Cameras tab → the feed
plays; the Home camera tile shows the lifecycle pill. Watch two minutes; note the bridge's
`lat[cap->enc]` p50/p95 and `consumers=1`.

### B4. Baseline numbers (fill the table in G)
```bash
docker stats --no-stream                                        # CPU % per container
tegrastats --interval 1000 | head -5                            # NVENC/GPU/CPU load line
./rig end-run                                                   # seal the baseline run
```
Record: core CPU %, bridge CPU %, recording MB/min, drops, webrtc p95 latency, topic Hz, the run id.

---

## C. The probe — does the modern userspace see the NVIDIA stack (15 min)

Runs against images, not the deployment; nothing here touches the rows.
```bash
cd ~/camera-service
tools/jp6-modern/probe.sh --images cam-core:jp6m,<cam-core image>,webrtc-bridge:jp6m    # csv,cdi x images
cat jp6m-results/summary.txt
```
For cdi mode first generate the spec (once): `sudo nvidia-ctk cdi generate --mode=csv --output=/etc/cdi/nvidia.yaml`.
Read each cell in the order [jp6-modern-userspace.md](jp6-modern-userspace.md) step 2 gives:
unresolved libraries → plugin load reasons → elements → throughput → bit-exact → webrtcsink. The
pinned-image cells must pass (they prove the probe on this host); the `jp6m` cells are the question.

**Decision:** csv green → part D in csv mode; only cdi green → D in cdi mode; neither → the Triage
section of the plan, and skip to F on the baseline.

---

## D. The real camera on the modern stack, inside the deployment (45 min)

The per-host file `/etc/rig/vehicle.local.yaml` takes an `env:` map that merges over the baked
`vehicle.yaml`'s (per key). Four lines swap the row's images and tell the bridges the 1.28 core
publishes unixfd — exactly what the bench deployment does:
```yaml
# /etc/rig/vehicle.local.yaml  (append; identity keys stay as provisioned)
env:
  CAM_TRANSPORT: unixfd
  CAM_CORE_IMAGE: cam-core:jp6m
  CAM_WEBRTC_IMAGE: webrtc-bridge:jp6m
  CAM_ROS2_IMAGE: ros2-bridge:jp6m
```
Then, through rig (NOT `run.sh`, whose compose-only scripts carry the baked digests):
```bash
cd $ART
./rig down <cam>
./rig new-run jp6m-csv
./rig up <cam>
./rig status
~/camera-service/tools/jp6-modern/stack-check.sh config/sensors/<cam>.yaml --project <cam>-vehicle-<id>
```
`stack-check.sh` prints the verdicts: core `healthy` on GStreamer 1.28, `recorder: encoder=hw-hevc-lossless`,
`plugin transport endpoint (unixfd)`, no ERROR lines; webrtc-bridge on 1.28 with `webrtcsink 0.15.3`
and `nvv4l2h264enc=rank …` in its inventory (add `GST_PLUGIN_FEATURE_RANK: nvv4l2h264enc:MAX` to the
webrtc-bridge params in the sensor config to make it the pick, then `./rig down/up <cam>`);
ros2-bridge with `CamUnixfdBridge`.

Then repeat B1's timestamp lines (same expectations — the source code is the same, the userspace
is not), B2's recording + the bit-exact test on the modern image:
```bash
docker run --rm --runtime nvidia --network host cam-core:jp6m tools/nvenc_lossless_test.py --frames 60   # csv
docker run --rm --device nvidia.com/gpu=all --network host cam-core:jp6m tools/nvenc_lossless_test.py --frames 60   # cdi
```
B3's bridges + dashboard (the ros2 bridge is the unixfd consumer now; the dashboard also gets the
new `webrtcsink`'s congestion control — watch the bridge's status lines for the bitrate settling),
and B4's numbers; `./rig end-run`.

**cdi mode**: with `/etc/cdi/nvidia.yaml` generated, add `CAM_PLATFORM: jp7` to the same `env:`
block (cam-up honours it over the provisioned platform and applies the runc + CDI overlay to the core
and the webrtc bridge), then `./rig down <cam>`, `./rig new-run jp6m-cdi`, `./rig up <cam>`,
`stack-check.sh` again. It is the JP7 shape and the one that would productize with the least code.

**Back to the baseline**: remove the `env:` block (or comment it out), `./rig down <cam>`, `./rig up <cam>`
— or `./run.sh up`, which is pinned and ignores it anyway.

---

## E. Robustness on the modern stack (30 min)

Each with the row up and the dashboard open; the expectation is in `./rig logs <cam>`:
1. **Camera loss**: unplug the camera cable for 20 s, plug it back. Expect `liveness: stream stalled … -> reopening`,
   backoff lines, `source reconnected … resuming capture`; the dashboard tile goes offline and resumes;
   an open recording session keeps writing into the same prefix (frames both sides of the gap in the CSV).
2. **Core restart**: `docker restart $C`. Bridges reconnect on their own (`webrtc` status resumes,
   `ros2 topic hz` recovers) — no bridge restart needed.
3. **Bridge restart**: `docker restart <cam>-vehicle-<id>-webrtc-bridge-1` → the dashboard tile recovers
   within ~15 s; the same for the ros2 bridge → the topic resumes.
4. **Row cycle**: `./rig down <cam> && ./rig up <cam>` while the run is open → recordings continue
   under the same run; `./rig runs` shows it still OPEN.
5. **Reboot** (if time): reboot the Orin; the `restart: unless-stopped` containers (or the systemd unit
   `provision.sh` installed) bring the deployment back; `./rig status` is green; the dashboard reconnects.
   Note whether the modern images came back (the local `env:` is read again at `rig up`; a reboot that
   restarts containers keeps whatever image they were created from).

Record: each recovery time, anything that needed a manual kick.

---

## F. The vehicle's runs, replay, and the dashboard's control widgets (60 min)

On whichever image set part D decided.
1. **Runs**: B and D already produced labelled runs; `./rig runs` lists them OPEN / sealed with the
   instance's recordings under `recordings/<cam>/`. `./rig up` with no run open auto-opens one — the
   safety net; deliberate sessions get a label.
2. **Replay**: `./rig replay <run-id>` — the instance comes up as a `replay` source of that run
   (reproduce mode: no names), the dashboard shows the replayed feed with its playback card
   (position, session), the ros2 bridge republishes it; `--wall-clock` / `--from` / `--to` / `--session`
   per `./rig replay --help`, and `./rig replay <run-id> <cam>` puts the live instance under test fed
   from its own recording. See [PLAYBACK.md](PLAYBACK.md).
3. **bag_recorders widget** (needs rig's bag logger row): the dashboard lists it; pause → `paused`,
   resume, split (a new mcap appears under the open run). Writing control only — no run control from
   the dashboard (`rig_actuate: false`).
4. **services widget**: a `services` widget listing `…/rosbag2_recorder/pause` and `…/resume`; the
   `resume` form shows `resume_time` as JSON; a bad integer is refused; a call shows the reply.
5. **format readouts**: a panel row with `format: "lat {latitude:.6f} lon {longitude:.6f}"` on a real
   NavSatFix topic if the vehicle has one.
6. **Bus debug**: the attachment column must read `rmw ✓ seq N · plain` — this fleet's rmw_zenoh
   layout; a mismatch kills the server node on the first service call (dashboard TESTING.md).

---

## G. Capture and write-up (20 min)

Collect into one directory and attach it to the branch's write-up:
- `jp6m-results/` from C (all logs + `summary.txt`).
- `stack-check.sh` output for B (baseline: expect the NVENC and unixfd lines to differ), D csv, D cdi.
- `./rig status` and `./rig runs` after each part; `./rig logs <cam> > core-<mode>.log` and the two
  bridges' `docker logs` per configuration.
- One recording's `.csv` + `.json` sidecar per run, and the `nvenc_lossless_test.py` output per image.
- `docker stats --no-stream` and one `tegrastats` line per configuration, under load.
- Dashboard screenshots: the tile playing, the bridge's latency line, the Bus debug attachment column.
- `metadata.yaml` of the artifact and `/etc/rig/vehicle.local.yaml` as used (minus anything private).

| | baseline (pinned) | `jp6m` csv | `jp6m` cdi |
|---|---|---|---|
| GStreamer in the core | 1.20 | 1.28 | 1.28 |
| nv plugins load / elements present | | | |
| timestamp source (`ptp_chunk`?) / PTP locked | | | |
| recorder encoder / MB per min | | | |
| bit-exact NVENC | | | |
| transport core → bridges | shm+header | | |
| webrtcsink version / encoder picked | 0.13.7 / | | |
| webrtc p50 / p95 cap→enc latency | | | |
| ros2 topic Hz | | | |
| core CPU % / bridge CPU % | | | |
| drops over 5 min | | | |
| camera-loss recovery (E1) | | | |
| run id | | | |
| verdict | | | |

**Decision rule.** jp6m is adopted for JP6 if a mode has: plugins loading, `hw-hevc-lossless` chosen
and bit-exact, PTP provenance unchanged from the baseline, no drops, and the dashboard stream at
least as good on latency. Then the productizing list at the end of
[jp6-modern-userspace.md](jp6-modern-userspace.md) applies — plus, for the fleet, `jp6m` in
`rigging.yaml`'s `build.platforms` so `rig build` / `rig bake` produce and pin the images instead of
a per-host `env:` swap. Otherwise the baseline stays, and the triage notes say what to fix.

---

## Appendix — without rig (a bare camera-service checkout)

The same parts with the standalone launcher: `tools/zenohd.sh up` once; `./cam-up config/sensors/<cam>.yaml up -d`
for the baseline (auto-detects jp6); `tools/jp6-modern/cam-up-jp6m …` for D (csv default, `JP6M_MODE=cdi`);
container project `cam_<name>`; recordings under the config's `recording.output_dir` / `./recordings`;
the dashboard via its own `dash-up config/infra/dashboard.example.yaml up -d`. No runs, no replay by
run id (a `camera.type: replay` config pointing at the recording directory instead — [PLAYBACK.md](PLAYBACK.md)).
