# camera-service on the JP6 hardware — the full test procedure

One day, one JetPack 6 Orin with real sensors. Seven parts, in this order; each ends with what to
record. Parts A–B are today's stack (the baseline every later number is compared to), C–E are the
modern-userspace experiment ([jp6-modern-userspace.md](jp6-modern-userspace.md)), F is the vehicle
shape under `rig` with the dashboard's control widgets, G is the write-up. **Stop and capture at the
first part that fails; the later parts assume the earlier ones.** Time budgets are for one camera.

Conventions: `<cam>` is your sensor config name (`config/sensors/<cam>.yaml`), `<iface>` the camera
NIC, `$C` the core container (`docker ps --format '{{.Names}}' | grep core-driver`). The core image's
ENTRYPOINT is `python3`, so non-python tools inside it need `--entrypoint`.

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
df -h /data 2>/dev/null || df -h ~             # room for recordings (lossless 5 MP mono ~ 1-2 GB/min)
```
Record: JetPack/L4T version, toolkit version, free disk.

### A2. Camera network + PTP (GigE only)
```bash
sudo ip link set <iface> mtu 9000
sudo sysctl -w net.core.rmem_max=33554432
sudo ptp4l -i <iface> -m &        # the host is the PTP grandmaster the camera slaves to
sudo phc2sys -a -r &
ping -c 2 <camera-ip>
```
Find the camera and its vendor's chunk/PTP feature names (the config needs them if they differ):
```bash
docker run --rm --network host --entrypoint arv-tool-0.8 cam-core:jp6                 # "<Vendor>-<serial>"
docker run --rm --network host --entrypoint arv-tool-0.8 cam-core:jp6 features | grep -iE "chunk|1588|ptp"
```
Record: camera id string, PixelFormat, whether ChunkTimestamp / ChunkFrameID / GevIEEE1588 exist.

### A3. The repo and the images
```bash
git clone https://github.com/christomaszewski/camera-service && cd camera-service
git checkout jp6-modern                        # the experiment branch (everything below is on it)
```
Images — two sets. **Baseline** (today's JP6 stack): `cam-core:jp6` (l4t base, GStreamer 1.20),
`ros2-bridge:jp6`, `webrtc-bridge:jp6`. **Modern** (`:jp6m`): `cam-core`, `cam-dev`, `ros2-bridge`,
`webrtc-bridge` on Ubuntu 26.04. Load the tarballs a bench build produced:
```bash
docker load < jp6-baseline-images.tar
docker load < jp6m-images.tar
docker images | grep -E "cam-core|cam-dev|ros2-bridge|webrtc-bridge"
```
(or pull from your registry: `export CAM_REGISTRY=<host:port>; ./cam-up config/sensors/<cam>.yaml pull`
for the baseline, `tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml pull` for jp6m). Building
on the Orin works too but costs ~10 min for the core (Aravis from source) and ~45 min for the webrtc
bridge (Rust) — not for the test day.

### A4. The sensor config
```bash
cp core-driver/config/sensors/cam_gige.yaml config/sensors/<cam>.yaml   # or cam_usb.yaml for a UVC camera
```
Set `name`, `gige.camera_id` (the id from A2), `gige.pixel_format` (Mono8 / BayerRG8 = the HW lossless
path; Mono16 = FFV1), `camera.frame_rate`, the vendor chunk names if A2 showed different ones,
`recording.encoder: auto`, and the webrtc-bridge `port`. Leave both bridges enabled. Check it:
```bash
python3 tools/sensor_env.py config/sensors/<cam>.yaml         # the derived compose env, no surprises
./cam-up config/sensors/<cam>.yaml config | head -40           # the rendered stack (jp6 auto-detected)
```

### A5. The ROS 2 router (once per host)
```bash
tools/zenohd.sh up && tools/zenohd.sh status
```

---

## B. Baseline — today's JP6 stack with the real camera (45 min)

This is L2–L4 of [jetpack7-bringup.md](jetpack7-bringup.md) on JP6: the numbers every later row
is compared to.

### B1. Up, and the timestamp spine
```bash
./cam-up config/sensors/<cam>.yaml up -d
./cam-up config/sensors/<cam>.yaml ps                                   # core: healthy (up to ~60 s)
./cam-up config/sensors/<cam>.yaml logs core-driver | grep -iE "chunk|1588|ptp|timestamp source|encoder=|transport endpoint|health:" | head -20
```
Expect: `chunk mode enabled`, `GevIEEE1588Status=Slave` (within `ptp_lock_timeout_s`),
`Active timestamp source = ptp_chunk`, `recorder: encoder=hw-hevc-lossless` (Mono8/Bayer8) or `ffv1`
(16-bit), `plugin transport endpoint (shm+header)` (this is the 1.20 core), `health: frames=N, no drops`
climbing at the frame rate.

If PTP never locks: the [PTP experiment](ptp-timestamp-experiment.md) is exactly this camera's
question — run it now (it only needs the sidecar CSV from B2) and note the provenance the core fell
back to (`camera` or `system`).

### B2. A recording, and is it lossless
Open a 60 s session, close it, and check the files:
```bash
C=$(docker ps --format '{{.Names}}' | grep "<cam>.*core-driver")
docker kill -s USR1 $C; sleep 60; docker kill -s USR2 $C       # SIGUSR1 opens a session, SIGUSR2 finalizes it
docker logs $C 2>&1 | grep -E "session .* (open|finalized)|sidecar" | tail -4
```
The session's dir is in the `finalized` line (`<data root>/recordings/<cam>/<prefix>-<stamp>-*.mkv`
+ `.csv` + `.json`). Then:
```bash
f=$(ls -t <dir>/<prefix>-*-00000.mkv | head -1)
docker exec $C gst-discoverer-1.0 "$f" | grep -E "Duration|video|Width|Height"
head -3 <dir>/<prefix>-<stamp>.csv                              # chunk_ns / camera_ns / system_ns per frame
python3 -c "import json;d=json.load(open('<dir>/<prefix>-<stamp>.json'));print({k:d[k] for k in ('encoder','ptp_synced','frames','timestamp_source') if k in d})"
```
Lossless proof, on the exact recorder fragment (the CSV mount grants NVENC to this container):
```bash
docker run --rm --runtime nvidia --network host cam-core:jp6 tools/nvenc_lossless_test.py --frames 60
```
Expect `NVENC LOSSLESS PASS`, `60/60 frames bit-exact`. Record: encoder, file size per minute, PASS.
(README "Post-processing" has the decode-and-compare recipe for a real frame.)

### B3. The bridges and the dashboard
```bash
docker exec $(docker ps --format '{{.Names}}' | grep "<cam>.*ros2-bridge") bash -c \
  "source /opt/ros/lyrical/setup.bash; ros2 topic hz --no-daemon /<cam>/image_raw"     # the camera's fps
docker logs $(docker ps --format '{{.Names}}' | grep "<cam>.*webrtc-bridge") 2>&1 | grep -E "inventory|status:" | tail -2
python3 plugins/webrtc-bridge/tools/discovery_probe.py --connect tcp/localhost:7447       # EVENT PUT fleet/... + DESCRIPTOR_OK
```
Dashboard (from its repo, same host): `cd ../dashboard && ./dash-up config/infra/dashboard.example.yaml up -d`,
open `http://<orin-ip>:8080`, Cameras tab → the feed plays. Watch two minutes; note the bridge's
`lat[cap->enc]` p50/p95 and `consumers=1`.

### B4. Baseline numbers (fill the table in G)
```bash
docker stats --no-stream                                        # CPU % per container
tegrastats --interval 1000 | head -5                            # NVENC/GPU/CPU load line
```
Record: core CPU %, bridge CPU %, recording MB/min, drops, webrtc p95 latency, topic Hz.

```bash
./cam-up config/sensors/<cam>.yaml down
```

---

## C. The probe — does the modern userspace see the NVIDIA stack (15 min)

```bash
tools/jp6-modern/probe.sh                            # csv,cdi x cam-core:jp6m, cam-core:jp6, webrtc-bridge:jp6m
cat jp6m-results/summary.txt
```
For cdi mode first generate the spec (once): `sudo nvidia-ctk cdi generate --mode=csv --output=/etc/cdi/nvidia.yaml`.
Read each cell in the order [jp6-modern-userspace.md](jp6-modern-userspace.md) step 2 gives:
unresolved libraries → plugin load reasons → elements → throughput → bit-exact → webrtcsink. The
`cam-core:jp6` cells must pass (they prove the probe on this host); the `jp6m` cells are the question.

**Decision:** csv green → part D in csv mode; only cdi green → D in cdi mode; neither → the Triage
section of the plan, and skip to F on the baseline images.

---

## D. The real camera on the modern stack (45 min)

```bash
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml up -d          # csv mode (JP6M_MODE=cdi for cdi)
tools/jp6-modern/cam-up-jp6m config/sensors/<cam>.yaml ps
tools/jp6-modern/stack-check.sh config/sensors/<cam>.yaml
```
`stack-check.sh` prints the verdicts: core `healthy` on GStreamer 1.28, `recorder: encoder=hw-hevc-lossless`,
`plugin transport endpoint (unixfd)`, no ERROR lines; webrtc-bridge on 1.28 with `webrtcsink 0.15.3`
and `nvv4l2h264enc=rank …` in its inventory (add `GST_PLUGIN_FEATURE_RANK: nvv4l2h264enc:MAX` to the
webrtc-bridge params to make it the pick and re-`up`); ros2-bridge with `CamUnixfdBridge`.

Then repeat B1's timestamp lines (same expectations — the source code is the same, the userspace
is not), B2's recording + bit-exact test on the modern image:
```bash
docker run --rm --runtime nvidia --network host cam-core:jp6m tools/nvenc_lossless_test.py --frames 60   # csv
docker run --rm --device nvidia.com/gpu=all --network host cam-core:jp6m tools/nvenc_lossless_test.py --frames 60   # cdi
```
B3's bridges + dashboard (the ros2 bridge is the unixfd consumer now; the dashboard also gets the
new `webrtcsink`'s congestion control — watch the bridge's status lines for the bitrate settling),
and B4's numbers. The dashboard's camera tile can drive the session instead of signals: ● activates,
the pill reads `active`, ● again deactivates.

If D passed in csv mode, run it once more in cdi mode (`down`, then `JP6M_MODE=cdi … up -d`,
`stack-check.sh`); it is the JP7 shape and the one that would productize with the least code.

---

## E. Robustness on the modern stack (30 min)

Each with the stack up and the dashboard open; the expectation is in the core's log:
1. **Camera loss**: unplug the camera cable for 20 s, plug it back. Expect `liveness: stream stalled … -> reopening`,
   backoff lines, `source reconnected … resuming capture`; the dashboard tile goes offline and resumes;
   an open recording session keeps writing into the same prefix (frames both sides of the gap in the CSV).
2. **Core restart**: `docker restart $C`. Bridges reconnect on their own (`webrtc` status resumes,
   `ros2 topic hz` recovers) — no bridge restart needed.
3. **Bridge restart**: `docker restart <webrtc-bridge>` → the dashboard tile recovers within ~15 s;
   `docker restart <ros2-bridge>` → the topic resumes.
4. **Writer-side restart for a shm/unixfd input** (only if you run a `ros2-source`-fed instance — see
   [plugins/ros2-source](../plugins/ros2-source/README.md)): restart it; the core logs `the writer closed
   the stream; reconnecting` and reconnects.
5. **Reboot** (if time): reboot the Orin; the `restart: unless-stopped` stacks and the router come back;
   the dashboard reconnects.

Record: each recovery time, anything that needed a manual kick.

---

## F. The vehicle shape: rig, runs, replay, the control widgets (60 min, optional)

Install rig from the release deb (`sudo dpkg -i rig_0.2.53_all.deb`), make a deployment
(`rig init ~/veh --vehicle-id 1`, `rig add camera-service`, point the row at your `<cam>.yaml`, a
`zenoh-router` infra row, a `dashboard` infra row with a Home config — the bench deployment in
`playback/viewer` is the template), then:
```bash
rig doctor && rig up                       # router -> sensors -> dashboard
rig status
```
1. **A run**: `rig run new` (or the dashboard's rig widget if `rig_actuate: true`), activate the camera
   from the dashboard tile, record two minutes, deactivate, `rig run end`. The run registry
   (`rig runs`) shows it with the camera's recordings under `recordings/<cam>/`.
2. **Replay**: `rig replay <run-id>` — the camera-service instance comes up as a `replay` source of that
   run (reproduce mode: no names), the dashboard shows the replayed feed with its playback card
   (position, session), and a `rig replay --live` check that it is paced. See the rig-sensor-replay
   plan and [PLAYBACK.md](PLAYBACK.md).
3. **bag_recorders widget**: with rig's bag logger row up, the dashboard's `bag_recorders` widget lists it;
   pause → `paused`, resume, split (a new mcap appears). Writing control only — no run control from
   the dashboard.
4. **services widget**: a `services` widget listing `…/rosbag2_recorder/pause` and `…/resume`; the
   `resume` form shows `resume_time` as JSON; a bad integer is refused; a call shows the reply.
5. **format readouts**: a panel row with `format: "lat {latitude:.6f} lon {longitude:.6f}"` on a real
   NavSatFix topic if the vehicle has one.
6. **Bus debug**: the attachment column must read `rmw ✓ seq N · plain` — this fleet's rmw_zenoh
   layout; a mismatch kills the server node on the first service call (dashboard TESTING.md).

Run F on whichever image set part D decided; the jp6m set under rig means the four `env:` lines the
bench uses (`CAM_TRANSPORT: unixfd` and the three `CAM_*_IMAGE` names, `vehicle.yaml`).

---

## G. Capture and write-up (20 min)

Collect into one directory and attach it to the branch's write-up:
- `jp6m-results/` from C (all logs + `summary.txt`).
- `stack-check.sh` output for B (baseline: expect the NVENC and unixfd lines to differ), D csv, D cdi.
- `docker logs` of the core and both bridges for each configuration (`docker logs $C > core-<mode>.log 2>&1`).
- One recording's `.csv` + `.json` sidecar per configuration, and the `nvenc_lossless_test.py` output.
- `docker stats --no-stream` and one `tegrastats` line per configuration, under load.
- Dashboard screenshots: the tile playing, the bridge's latency line, the Bus debug attachment column.

| | baseline `jp6` | `jp6m` csv | `jp6m` cdi |
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
| verdict | | | |

**Decision rule.** jp6m is adopted for JP6 if a mode has: plugins loading, `hw-hevc-lossless` chosen
and bit-exact, PTP provenance unchanged from the baseline, no drops, and the dashboard stream at
least as good on latency. Then the productizing list at the end of
[jp6-modern-userspace.md](jp6-modern-userspace.md) applies (a real `jp6m` platform in `cam-up`,
`rigging.yaml`, the bridges' transport selection). Otherwise the baseline stays, and the triage notes
say what to fix.
