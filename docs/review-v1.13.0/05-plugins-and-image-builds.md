# 04 — Plugins (bridges) and image build: review of camera-service v1.13.0 (6d3af8b)

Scope: `plugins/{webrtc-bridge,ros2-bridge,ros1-bridge,ros2-source}`, `plugins/README.md`, `core-driver/Dockerfile*`,
`core-driver/requirements.txt`, `tools/{build-images.sh,rig-build.sh,gstreamer/*,package-launch-surface.sh,zenohd.sh,roscore.sh}`,
`docs/fleet-build-contract.md`. Cross-checked against `core-driver/cam_driver/transport.py`, `gst_frame.py`, `pipeline.py` (build section),
`formats.py`, `tools/sensor_env.py` (the env the bridges receive).

No tracked file in the tree was modified. The experiments cited below (E1–E12) were throwaway scripts run outside the repository; each is described where it is cited and indexed at the end of section 5.

Severity count: critical 0 · high 2 · medium 12 · low 11 (25 findings; PB-10 and PB-18 sit on the low/medium boundary and are counted as medium).
Evidence level: 14 findings rest on something executed here (PB-2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 19, 21, 25, and the GStreamer half of PB-1);
PB-1 and PB-3 additionally depend on upstream ROS launcher behaviour that could not be run (no ROS) and say so; PB-23 and one bullet of PB-19 are
labelled suspected.

Environment used for experiments: GStreamer 1.24.2 + PyGObject under `/usr/bin/python3.12` (shm, unixfd, bayer2rgb, videoconvert
present; no webrtcsink, no ROS, no Docker daemon). "confirmed-by-running" below means run in that environment.

Confidence labels: **confirmed-by-running** / **confirmed-by-reading** (path traced in this repo) / **suspected** (depends on
behaviour of an upstream component I could not execute here; what would confirm it is stated).

---

## 1. Defects

### PB-1 — ROS 2 bridge stays "Up" forever with no node when its first connect to the core fails  (HIGH)

- Where: `plugins/ros2-bridge/src/cam_bridge_base.cpp:156-159` (throw), `cam_header_bridge.cpp:74-77` / `cam_unixfd_bridge.cpp:76-79`
  (constructor calls `start_pipeline()`), `plugins/ros2-bridge/launch/bridge.launch.py:114-117` (no `on_exit`), `plugins/ros2-bridge/entrypoint.sh:21-22`,
  `plugins/ros2-bridge/compose.yml:79-81`.
- What is wrong: the bridge's whole recovery model is "exit the process, let `restart: unless-stopped` reconnect" (`cam_bridge_base.cpp:160-165`).
  That only holds once the pipeline is PLAYING. If the *initial* `gst_element_set_state(PLAYING)` fails, the component constructor throws:
  ```cpp
  if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    throw std::runtime_error("pipeline failed to reach PLAYING (is the core up and serving " +
                             socket_path_ + "?)");
  }
  ```
  The component is loaded into `component_container_mt` by `ros2 launch`. `rclcpp_components::ComponentManager::on_load_node` catches a constructor
  exception, logs "Component constructor threw an exception", answers the LoadNode service with `success=false`, and **keeps the container process
  spinning**; `launch_ros`' `LoadComposableNodes` only logs "Failed to load node ...". Nothing exits, so Docker's restart policy never fires and there
  is no healthcheck on the service. The container is `Up`, publishes nothing, forever.
- Triggering scenarios (both are ordinary on a vehicle):
  1. **Stale socket file after an unclean stop.** The socket lives in the persistent external volume `cam_<name>_sock`. After power loss / SIGKILL /
     OOM the file `/tmp/cam/unixfd` (or `/tmp/cam/frames`) is still there. On the next boot `entrypoint.sh` sees it immediately:
     ```sh
     for _ in $(seq 1 150); do [ -S "$sock" ] && break; sleep 0.2; done
     ```
     and launches at once, while the core only removes the corpse and re-binds inside `StreamPipeline.build()` (`pipeline.py:341`, `:362`), i.e.
     *after* the camera has been opened (Aravis discovery, optional PTP wait — the compose healthcheck budgets 25 s for it). `connect()` to a socket
     file with no listener → ECONNREFUSED → state change FAILURE → throw → wedged. The same race happens on every `docker restart` of the core that
     ends in SIGKILL (the bridge `_exit`s on the bus ERROR, restarts in ~1 s, finds the old file).
  2. **Core not ready within 30 s** (camera unplugged/booting, PTP lock wait): the entrypoint prints "not present after 30s; starting anyway", the
     constructor throws, wedged. When the camera appears later the bridge never recovers.
- Evidence that the authors know the constructor can fail this way but only handled it in ROS 1: `plugins/ros1-bridge/src/cam_ros1_bridge.cpp:331-333`
  ```cpp
  } catch (const std::exception& e) {
    ROS_FATAL("%s", e.what());
    rc = 1;                         // startup failure must exit non-zero so the restart policy retries
  ```
  The ROS 2 composable-node port lost this (ROS 1 vs ROS 2 drift).
- Confidence: repo side **confirmed-by-reading**; the GStreamer half (connect to a stale socket file → `set_state` returns FAILURE for both `shmsrc` and
  `unixfdsrc`) is **confirmed-by-running** (see PB-1 experiment note in §5). The `rclcpp_components`/`launch_ros` half is upstream behaviour I could not
  execute here (no ROS) → to confirm end-to-end: `docker run` the ros2-bridge image against a volume containing a dead socket file
  (`python3 -c "import socket;s=socket.socket(socket.AF_UNIX);s.bind('/tmp/cam/unixfd')"` then exit) and observe `docker ps` still `Up` with
  "Failed to load node" in the log.
- Existing tests: none. `plugins/ros2-bridge/tools/bridge_test.sh` waits for the socket before starting the bridge (`:59`) and never restarts the producer.
- Suggested fix: do not throw out of the constructor for a connect failure. Either (a) retry `start_pipeline()` from a timer inside the node until
  it reaches PLAYING, or (b) `RCLCPP_FATAL` + `::_exit(EXIT_FAILURE)` exactly as the bus watcher does, so the restart policy applies; and add
  `on_exit=Shutdown()` to the `ComposableNodeContainer` plus a compose healthcheck (e.g. topic publisher count) as a backstop. Independently make the
  entrypoint probe a *listening* socket (try a connect) instead of `[ -S ]`.

### PB-2 — ros2-source (`raw`/`header` framing) silently serves on `<socket>.0` after an unclean stop; the core never gets a frame again  (HIGH)

- Where: `plugins/ros2-source/cam_ros2_source/node.py:93-115` (`Feeder.start`; the unixfd-only unlink is `:102-106`, the shm branch `:109-115`).
- What is wrong: only the unixfd branch removes a stale socket:
  ```python
  # unixfdsink will not rebind over a stale socket left by a killed writer
  try:
      os.unlink(s.socket)
  ```
  The shm branch goes straight to `shmsink socket-path="{s.socket}" wait-for-connection=false`. GStreamer's shmpipe answers `EADDRINUSE` by binding
  `<path>.0`, `.1`, … and reports success. The core documents exactly this failure for its *own* sinks and guards against it
  (`core-driver/cam_driver/pipeline.py:245-251`: "shmsink does NOT fail … it quietly binds `<path>.0`, reports a successful state change, and serves
  frames there -- while every consumer keeps dialling the CONFIGURED `<path>`"), but the fix was not mirrored into this writer, which owns the socket in
  the reverse direction.
- Triggering scenario: a `camera.type: shm` instance fed by ros2-source with `shm.framing: raw` (the default) or `header` (the only stamped framing
  available on JP6). Vehicle loses power, or the container is OOM-killed / `docker kill`ed. `/tmp/cam/in` survives in the `cam_<name>_sock` volume.
  On restart the node logs `ros2-source: <topic> -> /tmp/cam/in (header framing) ...` and counts "frames out", while actually bound to `/tmp/cam/in.0`.
  The core's shm source treats the dead `/tmp/cam/in` as "writer absent" (cheap retry, `docs/TRANSPORT.md:54`) indefinitely: no recording, no preview,
  no error on either side. Every further unclean stop adds `.1`, `.2`, …
- Confidence: **confirmed-by-running** for the shmsink behaviour (see §5 experiment E2), **confirmed-by-reading** for the missing unlink.
- Existing tests: none (`core-driver/tests/test_ros2_source.py` covers only `framing.py`; `test_shm_source.py` uses its own clean writers).
- Suggested fix: unlink the configured path (lexists, like `_clear_stale_socket`) for all three framings before building the output pipeline, and after
  PLAYING read back shmsink's `socket-path` property and fail hard if it differs from the configured one.

### PB-3 — ROS 1 bridge with `debayer: true`: the bridge's exit-to-reconnect never restarts the container  (MEDIUM)

- Where: `plugins/ros1-bridge/launch/bridge.launch:14-28`, `plugins/ros1-bridge/src/cam_ros1_bridge.cpp:206-236` and `:331-336`.
- What is wrong: recovery is "exit the process; the container restart reconnects" (`cam_ros1_bridge.cpp:206-210`, `::_exit(EXIT_FAILURE)` at `:234`;
  startup failure `rc = 1` at `:333`). PID 1's child is `roslaunch`, and the bridge `<node>` has neither `required="true"` nor `respawn="true"`:
  ```xml
  <node name="cam_ros1_bridge" pkg="cam_ros1_bridge" type="cam_ros1_bridge" output="screen">
  ...
  <node if="$(arg debayer)" name="debayer" pkg="nodelet" type="nodelet" args="standalone image_proc/debayer" output="screen">
  ```
  roslaunch only exits when *all* its processes have died (or a `required` one). With `CAM_DEBAYER=true` the `debayer` nodelet is a second, healthy
  process, so after the bridge exits roslaunch keeps running, the container stays `Up`, and nothing ever restarts the bridge.
- Triggering scenario: JP6 vehicle, Bayer camera, `ros1-bridge` plugin with `params.debayer: true`. The core restarts once (watchdog reconnect that
  rebuilds the pipeline, config change, crash). shmsrc posts ERROR → bridge `_exit(1)` → `/cam/image_raw` and `/cam/image_color` are silent until a
  human restarts the container. Same outcome if the first connect fails (PB-1's scenarios).
- Without debayer the bridge is roslaunch's only process, roslaunch exits ("all processes on machine have died") and the restart policy works — which
  is presumably why this was not noticed.
- Confidence: repo side **confirmed-by-reading**; roslaunch's "non-required node death does not end roslaunch while another process lives" is upstream
  behaviour not executed here (**suspected** until run: `CAM_DEBAYER=true`, start stack, `docker restart` the core, check `docker ps` + `rostopic hz`).
- Existing tests: none for ros1-bridge at all.
- Suggested fix: `required="true"` on the bridge node (roslaunch then tears everything down and the container restarts).

### PB-4 — Tight-row frames meet GStreamer-stride elements: sheared/garbage images and out-of-bounds reads when a row is not a multiple of 4 bytes  (MEDIUM)

- Contract being violated: the wire carries tightly packed rows (`formats.bytes_per_frame`, `cam_unixfd_bridge.cpp:27` "The core pushes tight
  (width-stride) buffers, so the converter reads map.data as-is"). GStreamer elements without a `GstVideoMeta` assume rows padded to 4 bytes
  (`GST_ROUND_UP_4`). The two agree only when `width * bytes_per_pixel % 4 == 0`. Nothing validates geometry anywhere (no check in `config.py`,
  the bridges, or `scale_plan.py`). All shipped example configs happen to use widths divisible by 4, which is why it has not shown up.
- Three concrete instances in this area:
  1. **ROS 2 JP7 debayer** — `plugins/ros2-bridge/src/cam_unixfd_bridge.cpp:91-95` builds `unixfdsrc ! bayer2rgb ! videoconvert ! video/x-raw,format=RGB ! appsink`
     and `CamBridgeBase::publish` (`cam_bridge_base.cpp:238-259`) then copies `width*3*height` bytes with `step = width*3`:
     ```cpp
     const size_t step = static_cast<size_t>(m.width) * bpp;
     const size_t need = step * m.height;
     if (!m.data || m.size < need) { ...short frame... }
     msg->data.assign(m.data, m.data + need);
     ```
     For width 1282 (Basler acA1300, or any ROI with width % 4 == 2) videoconvert's RGB rows are 3848 bytes, not 3846: the `size < need` guard passes
     (buffer is *larger*) and every row is shifted 2 bytes further → diagonally sheared, colour-rotated `rgb8`. In addition `bayer2rgb` itself reads the
     tight input with stride `ROUND_UP_4(width)`, i.e. `(4 - width%4) * height` bytes past the end of the mapped memfd.
     **confirmed-by-running** (E3): `1280x16` → appsink buffers 61440 bytes (= tight); `1282x16` → 61568 bytes vs the 61536 `publish()` copies, no bus
     error, bayer2rgb silently consumed a 20512-byte input where it addresses 20544.
  2. **WebRTC 16→8 normalize pump** — `plugins/webrtc-bridge/tools/bridge_stream.py:979-982` emits a tight GRAY8 buffer
     (`self.norm(...).tobytes()` → `Gst.Buffer.new_wrapped(out_bytes)`) into `videoconvert`. Together with `CAM_WEBRTC_MAX_SIZE`, `scale_plan.fit_within`
     (`scale_plan.py:54`) only rounds to *even*, so e.g. a 640x512 thermal camera with `CAM_WEBRTC_MAX_SIZE: 426` is planned to 426x340 → GRAY8 needs
     428-byte rows. **confirmed-by-running** (E4): 424x340 converts cleanly; 426x340 makes `videoconvert` post "Internal GStreamer error: code not
     implemented" (its *invalid video buffer* path) for every frame while still pushing an (unfilled) output buffer → the viewer gets garbage/black and
     the only trace is the throttled `pipeline warning` line (`bridge_stream.py:662-671`).
  3. **WebRTC default path for a mono/Bayer camera whose width % 4 != 0** — same `videoconvert`/`bayer2rgb` behaviour on the frames exactly as the core
     publishes them (`run.sh:254`, debayer is on by default for Bayer). Root cause is the core's publishing convention (DP-1 in `02-core-data-path.md`); listed
     here because the bridge is where it surfaces and where a guard is cheap.
- The hand-written YUV converter has its own, separate out-of-bounds read for odd geometries — see **PB-25**.
- Existing tests: none exercise a width that is not a multiple of 4 (`test_scale_plan.py` asserts evenness only).
- Suggested fix: make the convention explicit and enforce it — reject (or pad) geometries where `width*bpp % 4 != 0` at core build time and in the header
  pump; in `CamUnixfdBridge` take stride from `GstVideoMeta`/`gst_video_info_from_caps` and copy row by row; make `fit_within` round the width down to a
  multiple of 4 (or have the normalize pump pad rows); guard odd widths in `yuv_to_rgb8`.

### PB-5 — ros2-source: converted frames are emitted without size check/unpadding; at some pinned geometries every frame crashes the core instance  (MEDIUM)

- Where: `plugins/ros2-source/cam_ros2_source/node.py:165-173` (`_on_converted`) vs `:201-208` (the as-is path, which *does* check).
  ```python
  buf = sample.get_buffer()
  pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else 0
  self._emit(buf.extract_dup(0, buf.get_size()), self._pts_base + pts)
  ```
  The convert pipeline ends in `videoconvert ! videoscale ! <pinned caps> ! appsink`, whose buffers use GStreamer's padded strides; the core expects
  exactly `bytes_per_frame` and treats anything else as fatal: `core-driver/cam_driver/sources/shmsrc.py:161,181,197` → `SourceConfigChanged`
  ("the process exits for a restart", `docs/TRANSPORT.md:46-48`).
- Triggering scenario: `shm: {pixel_format: RGB, width: 854, height: 480}` (or GRAY8 at any width % 4 != 0, or I420/NV12 at e.g. 1366x768) fed from a
  topic that needs conversion (different size/encoding, or CompressedImage). First converted frame → core logs the mismatch and exits → restart →
  same again: a crash loop with a perfectly consistent config.
- Confidence: **confirmed-by-running** (E5, real `Feeder`, header framing): pinned RGB 640x480 → 921600 bytes (ok); RGB 854x480 → emitted 1230720 vs
  `frame_bytes` 1229760; GRAY8 854x480 → 410880 vs 409920; I420 1366x768 → 1575936 vs 1573632. Core side confirmed-by-reading.
- Existing tests: none (the test file covers `framing.py` helpers only; `frame_bytes` is compared to the core at 64x48).
- Suggested fix: in `_on_converted` map with `GstVideo.VideoFrame`/`VideoMeta` and repack rows tightly (per plane), then apply the same
  `len(pixels) != pinned.frame_bytes` guard as the as-is path; reject pinned geometries the transport cannot carry.

### PB-6 — ros2-source: Bayer handling contradicts its README in both directions  (MEDIUM)

- Where: `plugins/ros2-source/cam_ros2_source/node.py:201-214` (`push_image`), `:138-157` (`_ensure_convert`), `:159-163` (`_on_cv_error`);
  `plugins/ros2-source/README.md:50-52` ("a color topic cannot be turned into a mosaic (dropped, logged)").
- (a) **Colour topic into a Bayer-pinned instance is not dropped — it is converted to luma and shipped as if it were the mosaic.** `Pinned.raw_caps()`
  for `BayerRG8` is `video/x-raw,format=GRAY8` and `push_image` has no Bayer guard before `_ensure_convert`. Downstream, the core labels the plane
  `video/x-bayer` (unixfd) / the bridges debayer it → false-colour garbage recorded as sensor data.
  **confirmed-by-running** (E6a): pinned BayerRG8 640x480 + three `rgb8` messages → `emitted=[307200, 307200, 307200] dropped=0 converted=3`, log
  "converting video/x-raw,format=RGB,... -> video/x-raw,format=GRAY8,...".
- (b) **Bayer topic into a non-Bayer instance: one pipeline build + teardown per message, forever, and nothing counted as dropped.** The convert
  pipeline has no `bayer2rgb`, so `video/x-bayer` → `videoconvert` is not-negotiated; `_on_cv_error` then does `self.cv_caps = ""` with the comment
  "the next message rebuilds it" while logging "dropping until the input changes" — the very next (identical) message rebuilds. Each rebuild also leaks
  the previous pipeline's bus signal watch (`bus.add_signal_watch()` at `:152`, never removed).
  **confirmed-by-running** (E6b): 10 `bayer_rggb8` messages into pinned RGB → convert pipeline built 10×, `emitted=[]`, `dropped=0` (so the 10 s stats
  line reports "0 dropped" while delivering nothing).
- Existing tests: `test_pinned_format_decisions_mirror_the_core_parser` pins `matches()` only; neither path is tested.
- Suggested fix: in `push_image`, if `pinned.bayer` and the message is not that mosaic → `_drop_once`; add `bayer2rgb` ahead of `videoconvert` when the
  input is `video/x-bayer`; on a convert error remember the failing caps and stop rebuilding until they change; `remove_signal_watch()` on rebuild.

### PB-7 — ros2-source stamps arrival time but labels it `camera`  (LOW)

- Where: `plugins/ros2-source/cam_ros2_source/node.py:186-188` and `:246-253`; docs `node.py:23-25`, `README.md:47-48` ("a zero stamp means arrival (`system`)").
  ```python
  if stamp <= 0:
      stamp = time.time_ns()
  ...
  source = "camera"
  if s.transport == "header":
      buf = Gst.Buffer.new_wrapped(F.pack_header(stamp, frame_id, p.width, p.height, p.format, source) + pixels)
  ```
- Effect: a publisher that leaves `header.stamp` zero gets wall-clock arrival stamps recorded with `camera` provenance — the recording's sidecar then
  claims a capture-time quality it does not have. On unixfd the same substitution lands in `offset_end`.
- **confirmed-by-running** (E7): zero-stamp message → `pack_header(1794407938316257694, ..., 'camera')`.
- Also note the fallback uses `time.time_ns()` (wall), not the node clock; see §"Sim time" in section 5.
- Existing tests: header packing is tested with explicit sources; the zero-stamp path is not.
- Suggested fix: carry a `(stamp, source)` pair from `push_*` into `_emit` and pass `"system"` when the fallback fired.

### PB-8 — A ROS bridge plugin entry without `params:` loses frame_id / encoding / debayer (env derivation bug that lands in the bridges)  (MEDIUM)

- Where: `tools/sensor_env.py:276` and `:332-343` (reported here because it decides the bridge's `frame_id` and Bayer label),
  consumed by `plugins/ros2-bridge/compose.yml:44,48-49` and `launch/bridge.launch.py:79-81`.
  ```python
  by_name = {p["name"]: (p.get("params") or {}) for p in plugins}
  ...
  ros = by_name.get("ros2-bridge") or by_name.get("ros1-bridge")
  if ros is not None:
      env["CAM_ROS_TOPIC"] = ...; env["CAM_FRAME_ID"] = str(ros.get("frame_id", name)); env["CAM_ROS_ENCODING"] = ...
  ```
  An enabled plugin with no params is `{}`, which is falsy, so the `or` falls through to `None` and the whole block is skipped.
- Triggering scenario: two cameras whose configs list `- name: ros2-bridge / isolation: container` with no `params:` (all defaults wanted). Compose then
  applies its own defaults — `CAM_FRAME_ID=${CAM_FRAME_ID:-camera}`, `CAM_ROS_ENCODING=${CAM_ROS_ENCODING:-}` — so both publish
  `header.frame_id = "camera"` (TF frame collision; the launch file's intended default `frame_id = instance` at `bridge.launch.py:79` is dead code under
  compose because the variable is always set), and on JP6 a Bayer camera is published as `mono8` (the header only says GRAY8; the label comes solely
  from `CAM_ROS_ENCODING`, `cam_header_bridge.cpp:131-133`), so a downstream `image_proc` never debayers it.
- **confirmed-by-running**: `tools/sensor_env.py` on a config with a param-less ros2-bridge emits only `COMPOSE_PROFILES`/`CAM_INSTANCE`; adding
  `params: {topic: image_raw}` makes `CAM_ROS_TOPIC`, `CAM_FRAME_ID=cam_front`, `CAM_ROS_ENCODING`, `CAM_DEBAYER` appear.
- Existing tests: `core-driver/tests/test_sensor_env.py` has no param-less ROS bridge case; every shipped example carries `params: {topic: image_raw}`,
  which masks it.
- Suggested fix: `ros = by_name["ros2-bridge"] if "ros2-bridge" in by_name else by_name.get("ros1-bridge")`; and drop the `:-camera` default for
  `CAM_FRAME_ID` in both compose files so the launch default (instance name) applies.

### PB-9 — ROS 2 unixfd bridge hard-wires `bayer2rgb` from an env *prediction*; when the stream is not 8-bit `video/x-bayer` it crash-loops  (MEDIUM)

- Where: `plugins/ros2-bridge/src/cam_unixfd_bridge.cpp:87-97`
  ```cpp
  if (debayer_ && encoding_.rfind("bayer_", 0) == 0) {
    chain += "bayer2rgb ! videoconvert ! video/x-raw,format=RGB ! ";
  }
  ```
  `encoding_` is `CAM_ROS_ENCODING`, derived by `tools/sensor_env.py:62-70` from the *configured* pixel format — `bayer_<pat>16` for Bayer 10/12/16.
  The core publishes `video/x-bayer` only for 8-bit CFA (`core-driver/cam_driver/pipeline.py:347-352`: `if self._bayer and self._bits <= 8 ... else
  video/x-raw,format=<GRAY16_LE>`).
- Triggering scenarios (JP7 / jp6m / dev, `ros2-bridge` `params.debayer: true`):
  1. camera `pixel_format: BayerRG12` (or 10/16): hint is `bayer_rggb16`, stream is `video/x-raw,GRAY16_LE` → `bayer2rgb` cannot negotiate → bus ERROR
     → `::_exit(EXIT_FAILURE)` (`cam_bridge_base.cpp:177-189`) → restart → same, forever.
  2. config says BayerRG8 but the device/replay actually delivers mono (camera rejected the format, a proxy/replay with another format): same loop.
  The WebRTC bridge fixed exactly this class of failure with a runtime seam (`run.sh:73-86` "a front-end hardwired from CAM_BAYER dies not-negotiated
  (crash loop) whenever they disagree"); the ROS 2 bridge still has the hardwired version.
- **confirmed-by-running** (E8, the bridge's chain fed by appsrc): caps `video/x-raw,format=GRAY16_LE` → "Internal data stream error … not-negotiated (-4)";
  `video/x-raw,format=GRAY8` → same; `video/x-bayer,format=rggb` → frames flow.
- Related (LOW, same root): a >8-bit Bayer camera is never labelled Bayer on either transport — unixfd takes the encoding from caps (`mono16`), the header
  path only applies the hint when `pix.bytes_per_px == 1` (`cam_header_bridge.cpp:133`, `cam_ros1_bridge.cpp:305`). `bytes_per_pixel()`'s `bayer_*16`
  branch (`cam_bridge_base.cpp:15-18`, commented "unused-today") is dead code. Consumers receive `mono16` and cannot debayer.
- Existing tests: `test_transport.py::test_unixfd_bridge_covers_every_bayer_pattern` greps the 8-bit table only; no test of the debayer decision.
- Suggested fix: decide from the negotiated caps (link `bayer2rgb` on the first CAPS event, as `bridge_stream.py::_on_fmt_caps` does), or at least fall
  back to passthrough + a WARN when the caps are not `video/x-bayer`; apply the `bayer_*16` hint to 2-byte planes.

### PB-10 — WebRTC discovery descriptor usually advertises the env defaults (512x512@25 GRAY8), not the stream, on unixfd  (LOW–MEDIUM)

- Where: `plugins/webrtc-bridge/tools/bridge_stream.py:656-658` (advert fires on `cam_src` reaching PLAYING), `:150-193` (`fill_dims_from_caps` falls
  back to `CAM_WIDTH/HEIGHT/FPS/FORMAT`), `:681` (`_advertised = True`, one-shot; the payload is frozen in `StreamAdvertiser.__init__`,
  `zenoh_advertiser.py:44`). Defaults come from `plugins/webrtc-bridge/compose.yml:71-74` / `tools/sensor_env.py:392-395` (512/512/GRAY8/25 unless the
  plugin params set them — and the example configs tell users they no longer need to).
- What is wrong: the STATE_CHANGED→PLAYING message for `unixfdsrc` is dispatched from the main loop before the streaming thread has read the caps off
  the socket, so `pad.get_current_caps()` is still `None` and the descriptor is built from env. The code already knows this hazard for the headered
  path ("advertising here would stamp the descriptor from stale env defaults", `:652-655`) and defers the advert there, but not for unixfd — the
  default transport on JP7 / jp6m / dev.
- **confirmed-by-running** (E9: `videotestsrc 320x240@10 GRAY8 ! unixfdsink`, the real `bridge_stream.py`, compose-default env): over 20 starts the logged
  descriptor carried the real caps 5 times and `"width": 512, "height": 512, "fps": 25, "pixel_format": "GRAY8"` 15 times.
- Effect: fleet discovery (`fleet/<vehicle>/media/<sensor>`) tells viewers a wrong geometry/rate/pixel format for the lifetime of the bridge; the
  `CAM_WEBRTC_MAX_SIZE` downscale advertised at `:690-696` is then computed from the wrong source size too. Video itself is unaffected.
- Existing tests: `discovery_probe.py:24` explicitly does not assert width/height/fps/pixel_format.
- Suggested fix: advertise from the first CAPS event on `cam_src` (a pad probe, as `_on_fmt_caps` already has one), or re-publish the descriptor when the
  negotiated caps differ; never fall back to env geometry on a self-describing transport.

### PB-11 — WebRTC bridge leaks memory without bound when the status heartbeat is disabled (`CAM_WEBRTC_STATUS=0`)  (MEDIUM)

- Where: `plugins/webrtc-bridge/tools/bridge_stream.py:511-513`, `:537-539`, `:1057-1059` (appends) vs `:592-593` (the only drain, inside `_status_tick`),
  `:452-454` (the tick is not scheduled when the interval is 0).
  ```python
  self._status_iv = self._status_interval()
  if self._status_iv:
      GLib.timeout_add_seconds(self._status_iv, self._status_tick)
  ...
  lat_rx, self._lat_rx = self._lat_rx, []       # only here
  lat_enc, self._lat_enc = self._lat_enc, []
  ```
- Triggering scenario: operator sets `CAM_WEBRTC_STATUS: 0` in the sensor YAML to quiet the log (documented: "0 = off", `plugins/webrtc-bridge/compose.yml:134`).
  On unixfd and headered shm every frame then appends one float to each list forever: 2 × fps objects/s ≈ 1.6 kB/s at 25 fps ≈ 140 MB/day per camera,
  until the bridge is OOM-killed (on a Jetson that pressure is shared with the recorder).
- **confirmed-by-running** (E10, real `Bridge`, headered pump at ~100 fps, heartbeat off): `(len(_lat_rx), len(_lat_enc))` sampled each second =
  `(96, 96), (194, 194), (292, 292), (390, 390), (488, 488)`.
- Existing tests: none touch the latency lists.
- Suggested fix: bound them (`collections.deque(maxlen=…)`) or skip sampling when the heartbeat is off.

### PB-12 — Header pump forwards frames whose payload does not match the header's geometry  (LOW)

- Where: `plugins/webrtc-bridge/tools/bridge_stream.py:1022-1025` (only `payload <= 0` is rejected) and `header_transport.py:56-78` (no size rule).
  Both C++ consumers do check (`cam_bridge_base.cpp:241`, `cam_ros1_bridge.cpp:298-302`, `yuv_to_rgb8` `:42`) — consumer drift.
- Effect: a truncated/oversized frame is pushed with caps claiming WxH. `videoconvert` posts "invalid video buffer received" per frame yet still emits
  an output buffer (garbage to the encoder); with `bayer2rgb` at the seam there is no size check at all (it reads `ROUND_UP_4(w)*h` bytes from the
  shared shm area regardless). **confirmed-by-running** (E11: half-size payloads keep flowing through the pump; the only trace is the throttled
  `pipeline warning from fmt_next … invalid video buffer received`).
- Existing tests: `test_header_transport.py` covers header fields, not payload length.
- Suggested fix: add `expected_bytes(info)` to `header_transport.py` (mirror `formats.bytes_per_frame`) and drop + count mismatches in `_on_hdr_sample`.

### PB-13 — Signalling is unauthenticated, unencrypted and bound to every interface of the vehicle  (MEDIUM, deployment risk)

- Where: `plugins/webrtc-bridge/run.sh:218-221` (`gst-webrtc-signalling-server --host 0.0.0.0 --port "$PORT" &`), `compose.yml:22` (`network_mode: host`),
  `bridge_stream.py:108-115` (advertised as `ws://<hostname>:<port>`), `tools/zenohd.sh:43-45` / `tools/roscore.sh:33-34` (same posture for the
  router / master).
- Scenario: any host that can reach any interface of the vehicle computer (cellular modem, shop Wi-Fi, a passenger network) can open `ws://<veh>:8443`,
  list producers and start a session — i.e. watch every camera — and can also register a producer under the same `meta.name` the bridge uses, so a
  viewer selecting by `producer_id` may be served someone else's stream. No token, no TLS, no bind-address knob; the README carries no warning.
- Confidence: **confirmed-by-reading** (bind address and absence of any auth option); impact depends on network topology.
- Existing tests: `webrtc_test.sh` connects anonymously (by design).
- Suggested fix: add `CAM_SIGNALLING_BIND` (default the management interface or 127.0.0.1 behind a reverse proxy), support `--cert`/wss, document the
  exposure, and have `rig doctor` flag a wildcard bind.

### PB-14 — Transport/socket selection is spelled differently in every bridge  (LOW)

- `CAM_TRANSPORT`: the ROS 2 launch file and entrypoint accept `unixfd | header | shm(=header)` (`bridge.launch.py:69-73`, `entrypoint.sh:14-18`);
  `plugins/webrtc-bridge/run.sh:52-63,175-196` accepts `unixfd | shm | shm-raw` and its final `else` treats **anything else — including `header`, the name
  the ROS 2 side documents — as the legacy raw endpoint** `/tmp/cam/raw`. The same compose variable feeds both services, so `CAM_TRANSPORT=header`
  gives a working ROS bridge and a WebRTC bridge that waits 60 s for a socket that is not enabled by default and then restart-loops, with a message
  about `raw_endpoint` rather than about the unknown value. (cam-up itself only ever exports `unixfd`, `cam-up:143`, so this bites manual overrides.)
- Socket override: `plugins/ros2-bridge/entrypoint.sh:20` waits on `${CAM_TRANSPORT_SOCKET:-${CAM_SHM_SOCKET:-…}}` but the launch file reads only
  `CAM_TRANSPORT_SOCKET` (`bridge.launch.py:84`) → with `CAM_SHM_SOCKET` set the entrypoint waits for one path and the node opens another. And
  `plugins/ros2-bridge/compose.yml` does not pass `CAM_TRANSPORT_SOCKET` into the container at all (ros1 and webrtc do: `ros1-bridge/compose.yml:21`,
  `webrtc-bridge/compose.yml:48`), while `tools/sensor_env.py` never derives it from `transport.plugin_endpoint.socket_path` — a non-default plugin socket
  path in the sensor config cannot be followed by the ROS 2 bridge.
- Confidence: **confirmed-by-reading**. Tests: none. Fix: one shared vocabulary, reject unknown values loudly, pass/derive the socket consistently.

### PB-15 — ROS namespace is the raw instance name; names legal for Compose are illegal for ROS  (LOW)

- Where: `plugins/ros2-bridge/launch/bridge.launch.py:76-77` (`ns = "/" + instance.lstrip("/")`), `plugins/ros1-bridge/launch/bridge.launch:13`
  (`<group ns="$(arg instance)">`), `tools/sensor_env.py:266` (no validation of `name`).
- Scenario: instance `cam-front` (hyphen) or `2nd_cam` (leading digit) — fine for the compose project, the volume name and the WebRTC bridge — makes
  `ros2 launch` / roslaunch reject the namespace, so only the ROS bridge of that sensor restart-loops.
- Confidence: repo side **confirmed-by-reading**; the ROS name rules are upstream (not executed here). Fix: validate `name` in sensor_env or sanitize
  (`[^A-Za-z0-9_] -> _`, prefix a leading digit) in both launch files.

### PB-16 — Build: Aravis source tarball is downloaded and compiled with no integrity check  (MEDIUM, supply chain / reproducibility)

- Where: `core-driver/Dockerfile:57-59`
  ```dockerfile
  RUN cd /tmp \
      && wget -q "https://github.com/AravisProject/aravis/releases/download/${ARAVIS_VERSION}/aravis-${ARAVIS_VERSION}.tar.xz" \
      && tar xf "aravis-${ARAVIS_VERSION}.tar.xz" \
  ```
  The camera driver that ships in every vehicle's `cam-core` is built from whatever that URL returns; contrast `tools/gstreamer/build.sh:54`, which pins
  SHA-256 for every GStreamer archive and fails closed. The fetch also makes `cam-core` un-buildable offline and is not declared anywhere for the
  orchestrator. `git` in the same stage's apt list (`:52`) is unused (Aravis 0.8.36's only meson wrap, `gi-docgen`, is behind `-Ddocumentation`, default
  disabled — checked against the upstream tag).
- Confidence: **confirmed-by-reading**. Tests: `tools/test_gstreamer_archives.py` covers only the GStreamer extractor.
- Suggested fix: add the tarball to a `SHA256SUMS` and verify before `tar xf` (reuse `build.sh`'s `download_archive`/checksum pattern); drop `git`.

### PB-17 — Build: the webrtc-bridge image pulls six git repositories, none declared, and cargo is not `--locked`  (MEDIUM for offline/reproducible builds)

- Where: `plugins/webrtc-bridge/Dockerfile:71-88`
  ```dockerfile
  RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain ${RUST_TOOLCHAIN}
  RUN cargo install cargo-c --version ${CARGO_C_VER} --locked
  RUN git clone --depth 1 --branch ${GST_RS_TAG} https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git /tmp/gst-rs
  RUN cd /tmp/gst-rs && cargo cinstall -p gst-plugin-webrtc ... && cargo cinstall -p gst-plugin-rtp ... && cargo build --release --bin gst-webrtc-signalling-server
  ```
- Facts (I fetched `Cargo.lock` at tag `0.15.4` from the upstream GitLab to check): the release tag's lockfile has **70 packages from git sources**
  (and 1048 from crates.io). So the build needs, beyond the explicit clone:
  | repo | ref in the lock |
  |---|---|
  | `https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git` | tag `0.15.4` (explicit `git clone --branch`; a tag is movable, no commit pin) |
  | `https://gitlab.freedesktop.org/gstreamer/gstreamer-rs` | branch `0.25` @ `50d71e64ce6153a4dda06fc03f09eb2755fb9285` (39 crates) |
  | `https://github.com/gtk-rs/gtk-rs-core` | branch `0.22` @ `08a30b9cccafc82194cb24df13806a176f20b0ae` (16 crates) |
  | `https://github.com/gtk-rs/gtk4-rs` | branch `0.11` @ `2fcbc1544689df84b4f607d4dd6e7be668482494` (13 crates) |
  | `https://github.com/rust-av/ffv1.git` | rev `bd9eabfc14c9ad53c37b32279e276619f4390ab8` |
  | `https://github.com/rust-av/flavors` | rev `833508af656d298c269f2397c8541a084264d992` |
  `rigging.yaml`'s `build:` block declares none of them (nor the tarball hosts), so an orchestrated offline build has nothing to mirror.
  The first three are certain for `-p gst-plugin-webrtc`; the last three are other workspace members' dependencies — cargo resolves the whole workspace,
  so I expect them to be fetched too (**suspected**; confirm with a build log grep for "Updating git repository").
- `cargo cinstall` / `cargo build` run **without `--locked`** (only cargo-c's own install has it): if the committed lock ever disagrees with a manifest,
  cargo silently re-resolves and the `branch = "0.25"` / `"0.22"` dependencies float to the branch heads — the exact "broke in the field with no repo
  change" failure the Dockerfile's own comment (`:18-25`) describes for cargo-c.
- `curl … sh.rustup.rs | sh`: the installer script itself is unpinned and unverified (the toolchain version is pinned).
- Confidence: Dockerfile **confirmed-by-reading**; lockfile contents confirmed by fetching the upstream file; not built here (no daemon).
- Suggested fix: clone by commit SHA (`git fetch --depth 1 origin <sha>`), add `--locked` to all three cargo invocations, `cargo vendor` (or declare the
  repos + crates.io in the rig descriptor), and install rustup from a checksummed `rustup-init`.

### PB-18 — Build: `check.py` only verifies the WebRTC plugins *if webrtcsink already loads*  (LOW–MEDIUM)

- Where: `tools/gstreamer/check.py:21-23`, run as the last step of the webrtc-bridge `modern` target (`plugins/webrtc-bridge/Dockerfile:148-149`).
  ```python
  if Gst.ElementFactory.find("webrtcsink") is not None:
      for name in ("webrtcsrc", "nicesrc", "nicesink", "rtpgccbwe"):
          assert Gst.ElementFactory.make(name) is not None, f"missing bridge dependency: {name}"
  ```
  The script is shared with cam-dev (which has no webrtcsink), hence the `if`. But in the bridge image the plugin was compiled in the `builder` stage
  against the *distro* GStreamer dev packages and is loaded here into the 1.28.8 overlay; if it fails to load (blacklisted at registry scan, missing
  `/usr/local/lib/gstreamer-1.0` on the path, a dropped COPY) `find()` returns `None` and the build-time gate passes — producing a dev/replay
  `webrtc-bridge` whose pipeline fails at runtime with "no element webrtcsink".
- Also: the docstring says it fails the build "on mixed media-path plugins", but only the 27 listed elements are version-checked; `capssetter` (the
  `CAM_WEBRTC_DEBAYER=false` relabel, `format_adapt.py:45`) lives in gst-plugins-good's `debugutils`, which `build.sh:60` does not enable, so in the
  `modern` images it silently comes from the distro plugin dir (`GST_PLUGIN_SYSTEM_PATH`) — a mixed-version element on the media path that the check
  cannot see.
- Confidence: **confirmed-by-reading**. Suggested fix: `check.py --require webrtcsink` (argv/env) from the bridge Dockerfile; add `capssetter` to the
  list and `debugutils` to the gst-plugins-good features.

### PB-19 — `tools/rig-build.sh`: contract edge cases  (LOW)

- `RIG_BUILD_NO_CACHE` is tested for non-emptiness (`rig-build.sh:7`), so `RIG_BUILD_NO_CACHE=0` (or `false`) forces `--no-cache --pull`; the contract
  says `RIG_BUILD_NO_CACHE=1`. **confirmed-by-running** with a fake `docker`: `RIG_BUILD_NO_CACHE=0` → `docker build -t … --no-cache --pull -f …`.
- `RIG_BUILD_RESULT` is honoured only inside the `RIG_BUILD_PLATFORMS` branch (`:11-36`). With `RIG_BUILD_RESULT` set and `RIG_BUILD_PLATFORMS` unset the
  image is built and pushed, exit status 0, and **no result line is written** — the caller sees success with an empty report.
  **confirmed-by-running** (fake docker): `docker build` + `docker push`, "RESULT: (file not written)".
- With `PUSH=0` + one platform the helper passes `--load` and still appends `{"ref","digest"}` from `containerimage.digest` to the file documented as
  "published digests" (`docs/fleet-build-contract.md:19`) — a digest of a local image that exists in no registry. **confirmed-by-running** (fake docker).
- **suspected**: that same `--load` call carries an explicit `--provenance=mode=min` (`:22`). On an engine without the containerd image store, buildx
  refuses to load an image with attestations ("docker exporter does not currently support exporting manifest lists"), which would make the documented
  "`PUSH=0` under Buildx permits a single CPU member" path fail. Needs a real daemon to confirm (`PUSH=0 RIG_BUILD_PLATFORMS=linux/arm64 IMAGES=ros1-bridge
  tools/build-images.sh x v0-jp6` on a classic-store engine).
- The result file is opened in append mode and never truncated — a reused path accumulates lines from earlier runs.
- Existing tests: `tools/test_dev_defaults.py` checks the image matrix and the `buildx build` prefix only; `RIG_BUILD_NO_CACHE`, `RIG_BUILD_PULL`,
  `RIG_BUILD_RESULT`, `PUSH=0`, `--push/--load` are untested.

### PB-20 — `cam-core` Dockerfile is arm64-only but nothing stops (or flags) an amd64 build  (LOW)

- Where: `core-driver/Dockerfile:112-114`
  ```dockerfile
  ENV GI_TYPELIB_PATH=/usr/local/lib/aarch64-linux-gnu/girepository-1.0:/usr/lib/aarch64-linux-gnu/girepository-1.0 \
      LD_LIBRARY_PATH=/usr/local/lib/aarch64-linux-gnu:/usr/local/lib:... \
      GST_PLUGIN_PATH=/usr/local/lib/aarch64-linux-gnu/gstreamer-1.0
  ```
  On amd64 Aravis installs under `/usr/local/lib/x86_64-linux-gnu`, so the build succeeds and the image starts, but `gi.require_version("Aravis","0.8")`
  and `aravissrc` are not found. `rigging.yaml` restricts cam-core to arm64, but `tools/build-images.sh` applies `RIG_BUILD_PLATFORMS` to every image
  without checking the variant (`:108-122`), and `Dockerfile.dev`/the webrtc Dockerfile list both arch dirs. Also, with `L4T_MULTIMEDIA` set the `l4t`
  stage `apt-get download`s arm64-only packages and fails on amd64.
- Confidence: **confirmed-by-reading**. Fix: derive the multiarch dir at build time (`dpkg-architecture -qDEB_HOST_MULTIARCH`) or fail fast on non-arm64.

### PB-21 — Standalone launch bundle omits the router helpers that cam-up and the deploy doc call  (LOW)

- Where: `tools/package-launch-surface.sh:23-38` packages exactly `rigging.yaml`'s `launch_surface`, which lacks `tools/zenohd.sh` and `tools/roscore.sh`;
  `cam-up:267-268` executes `"$REPO/tools/zenohd.sh"` / `roscore.sh` for `--zenohd` / `--roscore`; `docs/deploy-without-rig.md:57,89` instruct
  `~/cam/tools/zenohd.sh up` and `./cam-up --zenohd … up -d` from the extracted bundle.
- **confirmed-by-running**: the tarball produced by the script (written to scratch) contains 30 entries; under `tools/` only `export-recordings`,
  `sensor_env.py`, `runtime_images.py` and `jp6-modern/*`. So on a vehicle provisioned from the bundle, `cam-up --zenohd` dies with "No such file" *after*
  creating the volume, and the documented manual command does not exist.
- Tests: `test_runtime_images.py::test_vendored_surface_includes_query_helper` checks `runtime_images.py` only. Fix: add both scripts to `launch_surface`.

### PB-22 — Helper scripts: the port knobs publish a port the daemon does not listen on  (LOW)

- `tools/zenohd.sh:35,45`: `CAM_ZENOH_PORT` only changes `-p "$PORT:$PORT"`; `rmw_zenohd` is started with its default config (7447). Same in
  `tools/roscore.sh:30,34`: `exec roscore` without `-p "$PORT"`. With a non-default port on a non-host network the published port has no listener.
  Both containers also run without `--init`, so PID 1 is the ROS wrapper (irrelevant for `down`, which uses `rm -f`). **confirmed-by-reading**.

### PB-23 — ROS 1 bridge aborts on an out-of-range header timestamp  (LOW, suspected)

- Where: `plugins/ros1-bridge/src/cam_ros1_bridge.cpp:282` — `msg.header.stamp.fromNSec(hdr.timestamp_ns);  // … fits ros::Time`.
  `ros::Time::fromNSec` throws `std::runtime_error("Time is out of dual 32-bit range")` above 2^32 s; this runs on the GStreamer streaming thread, where
  an uncaught exception is `std::terminate`. The producer masks rather than validates (`transport.py:94`, `framing.py:34`: `int(timestamp_ns) & _U64`),
  so a negative/sentinel stamp becomes ~1.8e19 ns. The ROS 2 bridge casts to `int64_t` and does not throw (`cam_header_bridge.cpp:136`) — drift.
  Not executed (no ROS 1 here); confirm with a header carrying `timestamp_ns = 0xFFFFFFFFFFFFFFFF`. Fix: range-check and drop/zero.

### PB-24 — Documentation / comments that contradict the code, and reader-side wire drift  (LOW)

- `core-driver/cam_driver/transport.py:15-17` (the contract): "bump `version` and read `header_len` for forward compatibility". Every consumer rejects
  `version != 1` outright (`cam_header_bridge.cpp:98`, `cam_ros1_bridge.cpp:265`, `header_transport.py:67-68`, `transport.py:108-109`), so following the
  rule drops 100 % of frames in every deployed bridge — behind a 1 Hz warning, with no bus ERROR, hence no restart and no health signal.
- Reader drift on `header_len`: the three bridges take pixels at `header_len` (`cam_header_bridge.cpp:114`, `cam_ros1_bridge.cpp:280`,
  `bridge_stream.py:1050`); the core's own reader ignores the field and slices at the constant (`sources/shmsrc.py:160`,
  `pixels = data[transport.HEADER_SIZE:]`; `transport.unpack_header` never validates or returns it). Identical today (every writer emits 36), but the
  forward-compat seam exists in the bridges only.
- `plugins/webrtc-bridge/Dockerfile:1` "reads the core's raw shm endpoint" — it reads the plugin endpoint (raw is the legacy hatch, `run.sh:17-21`).
- `plugins/ros2-bridge/README.md:76` documents `frame_id` default `camera`; the launch default is the instance name (`bridge.launch.py:79`) and the
  compose default overrides it back to `camera` (see PB-8).
- `plugins/webrtc-bridge/README.md:110` / `compose.yml:63-64` promise "even dimensions": `scale_plan.fit_within` returns `None` (no scaling) for a frame
  that already fits, so an odd-sized source goes to the encoder odd (`scale_plan.py:48-49`).
- `plugins/ros2-source/README.md:47-52` — zero-stamp provenance and "dropped, logged" for colour→mosaic (PB-7, PB-6).
- `tools/build-images.sh:67` error text lists `jp6|jp7|dev` while `jp6m` is accepted two lines above.
- `plugins/ros2-bridge/tools/bridge_test.sh:1-6` header says it validates the shm+header → CamHeaderBridge path; the script picks the transport from the
  producer image and with the default `cam-dev` (GStreamer 1.28) only ever exercises unixfd.
- `core-driver/requirements.txt` is referenced by no Dockerfile or script; the `eclipse-zenoh==1.9.0` pin is repeated in three Dockerfiles plus this
  dead file (four places to bump "one pin").

### PB-25 — `yuv_to_rgb8` reads past the mapped frame for odd width/height (both C++ bridges)  (LOW)

- Where: `plugins/ros2-bridge/src/cam_bridge_base.cpp:35-79`, duplicated verbatim at `plugins/ros1-bridge/src/cam_ros1_bridge.cpp:47-91`.
  The size guard and the loops disagree for odd geometry:
  ```cpp
  const size_t wh = static_cast<size_t>(w) * h, cw = w / 2, ch = h / 2;
  const size_t need = packed422 ? wh * 2 : (fmt == Yuv::NV12 ? wh + wh / 2 : (fmt == Yuv::NV24 ? wh * 3 : wh + 2 * cw * ch));
  if (src_size < need) return false;
  ...
  const int pair = (x >> 1) * 4;                       // 4:2:2: last pixel of an odd row reads row[2w+1]
  const size_t i = static_cast<size_t>(y / 2) * cw + (x / 2);   // 4:2:0: y = h-1, x = w-1 index a chroma row/col that `need` did not count
  ```
  Width and height come straight from the wire header (u16, only `w <= 0 || h <= 0` rejected) or from caps.
- **confirmed-by-running** (E12: the function copied verbatim, AddressSanitizer, source buffer of exactly `need` bytes): `YUY2 4x2`, `I420 4x2` clean;
  `YUY2 3x2` → heap-buffer-overflow "1 bytes after 12-byte region"; `I420 5x3` → overflow "0 bytes after 19-byte region"; `NV12 5x3` → overflow
  "0 bytes after 22-byte region".
- Scenario: a YUV source with an odd dimension (a cropped/ROI'd RTSP or USB stream, or a `camera.type: shm` writer pinned at e.g. I420 641x481 — the
  core's `bytes_per_frame` is `px*3//2`, the same undercount, so it forwards such frames). The bridge reads 1–2 bytes beyond the mapped memfd/shm
  block on the last rows of every frame: wrong edge pixels always, SIGSEGV when the frame ends on a page boundary (then the restart loop).
- Existing tests: none (see section 4 — the function is pure and trivially testable).
- Suggested fix: use `(w + 1) / 2`, `(h + 1) / 2` for chroma geometry and the 4:2:2 pair count in both `need` and the loops, or reject odd sizes.

---

## 2. Build inventory — every external fetch during image builds

None of the base images is pinned by digest; no apt package is version-pinned (the ROS bridges add `--no-upgrade`); only the GStreamer tarballs are
checksum-verified. Nothing below is declared in `rigging.yaml`'s `build:` block.

### Base images
| Image / stage | Reference | Pinned? |
|---|---|---|
| cam-core builder + runtime (`core-driver/Dockerfile:16,48,67`) | `${BASE_IMAGE}`: `nvcr.io/nvidia/l4t-base:r36.2.0` (jp6) · `ubuntu:24.04` (jp7) · `ubuntu:26.04` (jp6m) (`tools/build-images.sh:83,95,99`) | tag only |
| cam-core / webrtc `l4t` stage (`core-driver/Dockerfile:29`, `webrtc-bridge/Dockerfile:42`) | `ubuntu:22.04` — pulled on **every** build (the final stage `COPY --from=l4t`), even when `L4T_MULTIMEDIA` is empty | tag only |
| cam-dev (`core-driver/Dockerfile.dev:4-5,9`) | `ubuntu:26.04` (gstreamer stage) + `${BASE}` (default `ubuntu:26.04`; 22.04/24.04 with `--target distro`) | tag only |
| webrtc-bridge (`Dockerfile:11,30,61,91`) | `${BASE_IMAGE}` `ubuntu:24.04` (jp6/jp7) · `ubuntu:26.04` (jp6m/dev); `ubuntu:26.04` gstreamer stage (needed for `--target modern` only) | tag only |
| ros2-bridge / ros2-source (`ros2-bridge/Dockerfile:12,16,43`) | `ros:${ROS_DISTRO}-ros-base` (lyrical; `jazzy` fallback) or `RIG_BASE_IMAGE` verbatim (rig may pass a digest) | tag only unless rig pins |
| ros1-bridge (`ros1-bridge/Dockerfile:10,26`) | `ros:noetic-ros-base`, hard-coded: `build-images.sh:132` passes no args and `RIG_BASE_IMAGE` is not applied. Noetic / Ubuntu 20.04 are EOL (2025-05): no security updates, and the build depends on the EOL apt archives + ROS key staying valid (**suspected** build risk — not built here) | tag only |
| `tools/gstreamer/Dockerfile:5-6` | `cam-dev:jp6m` (local) + `ubuntu:26.04` | tag only |

### apt repositories
| Where | Repository | Pinned? |
|---|---|---|
| all Dockerfiles, `tools/gstreamer/build.sh:31-38` | the parent image's Ubuntu archive/ports (and packages.ros.org inside the ROS parents) | no versions |
| `core-driver/Dockerfile:34-40`, `webrtc-bridge/Dockerfile:47-53` (only when `L4T_MULTIMEDIA` is set) | `https://repo.download.nvidia.com/jetson/{common,t234} <release> main`; signing key `jetson-ota-public.asc` fetched with curl at build time (trust-on-first-use, no fingerprint check); packages `nvidia-l4t-{core,nvsci,multimedia-utils,multimedia,gstreamer}` | version only if `L4T_VERSION` is given |

### Git repositories
| Where | Repository | Ref |
|---|---|---|
| `webrtc-bridge/Dockerfile:75-76` | `https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git` | tag `${GST_RS_TAG}` = `0.15.4` (`--depth 1 --branch`; movable tag, no SHA) |
| cargo, via that tag's `Cargo.lock` | `https://gitlab.freedesktop.org/gstreamer/gstreamer-rs` | branch `0.25` @ `50d71e64ce6153a4dda06fc03f09eb2755fb9285` |
| 〃 | `https://github.com/gtk-rs/gtk-rs-core` | branch `0.22` @ `08a30b9cccafc82194cb24df13806a176f20b0ae` |
| 〃 (workspace-wide resolution; suspected fetched) | `https://github.com/gtk-rs/gtk4-rs` | branch `0.11` @ `2fcbc1544689df84b4f607d4dd6e7be668482494` |
| 〃 (〃) | `https://github.com/rust-av/ffv1.git` | rev `bd9eabfc14c9ad53c37b32279e276619f4390ab8` |
| 〃 (〃) | `https://github.com/rust-av/flavors` | rev `833508af656d298c269f2397c8541a084264d992` |

No other Dockerfile or build script clones git: cam-core (Aravis is a tarball; its only meson wrap is behind `-Ddocumentation`), cam-dev, ros2-bridge,
ros1-bridge, `tools/gstreamer/build.sh` (`--wrap-mode=nofallback`). CI-time only (not an image build): `.github/workflows/registry-release.yml:77`
`pip install git+https://github.com/christomaszewski/rig` and `certify.yml:104-108` (checkout of the same repo) — both unpinned HEAD.

### Tarballs / installers
| Where | URL | Checksum |
|---|---|---|
| `core-driver/Dockerfile:58` | `https://github.com/AravisProject/aravis/releases/download/0.8.36/aravis-0.8.36.tar.xz` | **none** (PB-16) |
| `tools/gstreamer/build.sh:46-55` (cam-dev `modern`, webrtc `modern`, `tools/gstreamer/Dockerfile`) | `https://gstreamer.freedesktop.org/src/<module>/<module>-1.28.8.tar.xz` ×7 (gstreamer, plugins-base/good/bad/ugly, libav, rtsp-server); fallback `https://deb.debian.org/debian/pool/main/g/<module>1.0/<module>1.0_1.28.8.orig.tar.xz` | SHA-256 pinned in `tools/gstreamer/SHA256SUMS`, verified before extraction, fails closed |
| `webrtc-bridge/Dockerfile:71-72` | `https://sh.rustup.rs` piped to `sh` (then Rust `1.96.0` from static.rust-lang.org) | script unverified; toolchain version pinned |

### pip / cargo
| Where | Dependency | Pinned? |
|---|---|---|
| `core-driver/Dockerfile:104`, `Dockerfile.dev:25`, `webrtc-bridge/Dockerfile:114` | `eclipse-zenoh==1.9.0` (PyPI wheel) | version yes, no `--require-hashes`; the pin is written in 3 Dockerfiles + the unused `core-driver/requirements.txt` |
| `webrtc-bridge/Dockerfile:74` | `cargo-c 0.10.23` | version + `--locked` |
| `webrtc-bridge/Dockerfile:84-88` | gst-plugin-webrtc, gst-plugin-rtp, gst-webrtc-signalling-server: 1048 crates.io packages + 70 git packages per the tag's lockfile | lockfile present upstream, but cargo is run **without `--locked`** (PB-17) |
| `tools/ci_pytest.sh` (CI container only) | `pytest>=8,<10`, `coverage>=7,<8`, `pytest-timeout>=2,<3` | ranges |

### Other build properties
- **Root:** no Dockerfile has a `USER`; every container (core, bridges, router, roscore) runs as root, with `ipc: host` and host networking.
- **Secrets in layers:** none found (no tokens/credentials in ARG/ENV/COPY). `NVIDIA_VISIBLE_DEVICES=all` etc. are not secrets.
- **Cache mounts:** none used (`--mount=type=cache` absent), so no cache-mount misuse — and no reuse: apt, cargo and the 7 GStreamer builds restart from
  zero on any uncached build; `tools/gstreamer/build.sh` is run in three separate Dockerfiles (identical stage text, so BuildKit can share it only
  within one builder cache).
- **Labels:** `io.rig.camera.replay-bindings=1` and `io.rig.replay.coordination=1` are on the final stage of `core-driver/Dockerfile:68-69` (all cam-core
  variants: jp6/jp7/jp6m share that stage) and on `Dockerfile.dev:10-11` in the `distro` stage, which `modern` extends (`FROM distro AS modern`,
  `:36`) — so both cam-dev targets carry them. `tools/gstreamer/Dockerfile` (`FROM ${BASE_IMAGE}`) inherits them from its parent. Confirmed by reading;
  not confirmed with `docker inspect` (no daemon).
- **Multi-arch:** cam-dev and webrtc-bridge list both multiarch lib dirs; cam-core is arm64-only (PB-20); the `l4t` stage creates
  `/l4t/usr/lib/aarch64-linux-gnu` unconditionally so the `COPY --from=l4t` works on amd64 (an empty aarch64 dir lands in amd64 images — harmless).
- **`--no-cache` / platforms / result:** see PB-19. `PLATFORM_FLAG` is correctly cleared when `RIG_BUILD_PLATFORMS` is set (`build-images.sh:108`;
  confirmed with the fake docker: one `--platform`).

---

## 3. Improvements & missing features (not defects)

ROS bridges
- **Reconnect in process** instead of exit-and-restart: both bridges tear the whole container down (and, on ROS 2, the composed `image_proc`) for every
  core restart; a retry timer around `start_pipeline()` would also remove PB-1/PB-3 by construction. Add a compose `healthcheck` to each bridge.
- **`sensor_msgs/CameraInfo` is never published** (plain `image_transport::Publisher`, not `CameraPublisher`); rectification / `image_proc` pipelines and
  most perception stacks expect `<ns>/camera_info` beside `image_raw`.
- **Frame id and timestamp provenance are parsed and discarded**: `FrameMeta.frame_id` (`cam_bridge_base.hpp:39`) is filled by both extractors and
  never used; `ts_source` is in the header and ignored. A subscriber cannot detect dropped frames or tell a PTP stamp from an arrival stamp. Publish
  them (diagnostics topic or a small side message).
- **ROS 1 ↔ ROS 2 drift (features/fixes present in one only):** publish-rate throttle (`CAM_ROS_PUBLISH_RATE` is exported for either bridge by
  `sensor_env.py:342-343` but only ROS 2 implements it and only its compose passes it — silently ignored on ROS 1); `convert_buf_` reuse (ROS 1
  allocates a `std::vector` per colour frame, `cam_ros1_bridge.cpp:288`); composable/intra-process debayer; `RIG_BASE_IMAGE`/`--no-upgrade` parent
  sharing (ROS 1 base is hard-coded); `stop_signal` (not needed for roslaunch); startup-failure exit (ROS 1 has it, ROS 2 lost it — PB-1);
  `required`/shutdown-on-exit (PB-3); negative-timestamp behaviour (PB-23). The header struct, `pixfmt_info` and `yuv_to_rgb8` are triplicated by hand
  (two C++ files + Python) — move the C++ into one header-only file shared by both packages and generate the table from `transport.py`.
- **Colour fidelity:** `yuv_to_rgb8` applies full-range BT.601 to everything; decoded H.264/H.265 (limited-range BT.709) is visibly off (the comment at
  `cam_bridge_base.cpp:25-26` concedes it). Either convert in GStreamer (`videoconvert` honours colorimetry) or publish the native encodings ROS has
  (`nv12`, `yuv422_yuy2`, `uyvy`) and skip the per-frame CPU conversion on the streaming thread.
- **QoS is fixed** to SensorData/best-effort (ROS 2) and queue 1 (ROS 1); make reliability/depth parameters (reliable DDS subscribers do not match a
  best-effort publisher).
- **Sim time:** accept `use_sim_time` (from `RIG_SIM_TIME`) as a launch parameter for graph consistency, and make the publish-rate throttle use frame
  stamps rather than `steady_clock` so a replay at `speed != 1` is decimated in stream time (see section 5 for what the clocks do today).
- 16-bit Bayer labelling (`bayer_*16`) on both transports (PB-9).

WebRTC bridge
- **There is no idle throttle in v1.13.0.** `_consumers` (`bridge_stream.py:330,626-632`) is only logged. With zero viewers the bridge still runs
  `bayer2rgb` + `videoscale` + `videoconvert` (+ the numpy normalize, + `textoverlay`) on every frame — the README itself calls 5 MP `videoconvert`
  the CPU burner. A valve/leaky drop ahead of the debayer when `_consumers == 0` (while keeping webrtcsink fed at ~1 fps so discovery/negotiation
  stays warm) would free a core per idle camera.
- `run.sh:218-221` backgrounds the signalling server and sleeps 1 s without checking it is alive; on host networking, if the port is already owned by
  another sensor's server, this bridge silently registers there. Check `kill -0`/the port and fail loudly; supervise the child (it is orphaned to tini).
- Re-advertise the descriptor when negotiated caps change (PB-10); cache invalidation for `_h264_caps_str` (`bridge_stream.py:754-755` computes the
  level once; a mid-stream geometry change — the header pump explicitly supports one, `:1046-1048` — keeps the old level).
- H.264 level math ignores Annex A's frame-dimension bound (`PicWidthInMbs`, `FrameHeightInMbs` ≤ `sqrt(8·MaxFS)`): very wide/tall strips (e.g.
  4096x128) get a level whose MaxFS/MaxMBPS fit but whose width does not. Add the check to `h264_level_for`/`level_covers`.
- A wrong `CAM_WEBRTC_X264_PRESET` is swallowed at DEBUG (`_configure_live_encoder.setp`); validate against the enum and warn.
- `StreamAdvertiser`: if the zenoh binding is missing the retry timer logs a WARNING every 5 s forever (`bridge_stream.py:723-728` +
  `zenoh_advertiser.py:62-65`); distinguish "binding absent" (stop retrying) from "router unreachable".

ros2-source
- Clear stale sockets for all framings and verify the bound path (PB-2); tight-row repack (PB-5); `bayer2rgb` in the convert chain (PB-6).
- Zero-stamp fallback should use the node clock (so it follows `/clock` under sim time) and carry `system` provenance (PB-7).
- `_qos()` mutates the process-global `qos_profile_sensor_data` (`node.py:288-290`); build a fresh `QoSProfile`.
- `ExternalShutdownException` (SIGTERM path in current rclpy) is not caught next to `KeyboardInterrupt` (`node.py:325-331`) → a traceback and exit 1
  on a clean stop that uses SIGTERM (suspected; SIGINT via `stop_signal` is the configured path).
- No `depends_on`/healthcheck; no metric of topic→socket latency or drops exposed beyond the 10 s log line (whose `dropped` misses PB-6b).

Build
- Pin base images by digest (rig can already pass one for the ROS parent); checksum Aravis; vendor/lock cargo; one `requirements.txt` actually used.
- Compile the bridge images in CI (today only `Dockerfile.dev` is built — section 4).
- Use BuildKit cache mounts for apt/cargo and build the pinned GStreamer once as its own image reused by cam-dev, webrtc `modern` and
  `tools/gstreamer/Dockerfile` (three copies of the same 7-module build today; `GST_BUILD_JOBS` is not even plumbed as a build ARG).
- Non-root users for the bridges (they need only the socket volume and `/dev/shm`).
- `rig-build.sh`: treat `0/false/no` as off; write `RIG_BUILD_RESULT` (or fail) on the non-Buildx path; drop provenance when `--load`ing.

---

## 4. Test gaps for this area

Confirmed state of automation: CI (`.github/workflows/certify.yml`) builds **only** `core-driver/Dockerfile.dev`, runs `tools/ci_test.sh`
(pytest over `core-driver/tests` + `plugins/webrtc-bridge/tools/test_*.py`), six core smoke scripts, the three `tools/test_*.py`, and `rig certify`.
It never builds `core-driver/Dockerfile`, `plugins/ros2-bridge/Dockerfile`, `plugins/ros1-bridge/Dockerfile` or `plugins/webrtc-bridge/Dockerfile`,
and never runs `bridge_test.sh`, `webrtc_test.sh`, `discovery_test.sh` or `check_encoder_setup.py`. Consequently:

- **The C++ bridges have no unit tests and are not even compiled in CI.** Neither `CMakeLists.txt` has `BUILD_TESTING`/gtest, neither `package.xml` a
  `test_depend`; there is no `test/` directory. The only automated checks are two source-greps in `core-driver/tests/test_transport.py:84-121`
  (every pixfmt `case N:` exists; every raw format/Bayer pattern string appears in `cam_unixfd_bridge.cpp`) — they pin the *presence of tokens*, not
  behaviour (a wrong encoding or bytes-per-pixel behind the right `case` passes).
  Testable **without ROS or hardware** (plain g++/gtest once the pure parts are split from the node, as E12 did by copying the function):
  `yuv_to_rgb8` (all six layouts, odd sizes, short buffers — would have caught PB-25); `pixfmt_info` vs `transport._CODE_TO_GST` values (encoding,
  bpp, endianness, not just labels); header parsing as a free function (magic/version/`header_len` bounds, short buffer, max u16 geometry) fed with
  bytes produced by `transport.FrameHeader.pack()` (a golden-bytes fixture shared with the Python tests); `caps_to_meta` (needs only libgstreamer);
  `bytes_per_pixel`; `throttle_drop` with an injected clock (average rate, stall re-anchor).
  Testable with GStreamer but no ROS: `CamBridgeBase` pipeline strings against a `videotestsrc ! shmsink`/`unixfdsink` producer; stride behaviour at
  width % 4 != 0 (PB-4); producer-restart → bus ERROR → exit code.
- **ros2-source `node.py` has no tests** — `core-driver/tests/test_ros2_source.py` covers `framing.py` only. `Feeder` imports no ROS (rclpy is imported
  inside `main()`), so everything in E5–E7 runs under plain PyGObject: as-is vs convert decision, converted frame size vs `pinned.frame_bytes`, Bayer
  cases, zero-stamp provenance, `_rel_pts` on backward stamps, stale-socket start (E2), output-error → `failed`. None exists.
- **No test restarts the producer under a bridge** (PB-1, PB-3) or starts a bridge against a stale socket file; `bridge_test.sh:59` waits for the
  socket first. No ROS 1 test of any kind. `bridge_test.sh` with the default `cam-dev` image only exercises unixfd; the JP6 header component is
  reached only if the tester supplies a GStreamer < 1.24 producer image.
- `bridge_stream.py` (1092 lines) has **no unit test**; only its pure helpers do. Untested and cheap to cover with `fakesink` standing in for
  webrtcsink (as E9–E11 did): descriptor geometry (PB-10), latency list bounds (PB-11), header pump (short payload, mid-stream geometry change, bad
  magic), `_on_fmt_caps` splice decisions, `_on_scale_caps` re-plan, normalize pump stride/short frame, exit code on bus ERROR, advertise retry.
  `discovery_probe.py:24` deliberately skips width/height/fps/pixel_format.
- `scale_plan`: no case where the result is even but not a multiple of 4 feeding a GRAY8 path (PB-4.2); no odd-source passthrough case.
- `h264_level`: no extreme-aspect case; `header_transport`: no payload-length rule to test (PB-12); wire-format: no test that a reader honours
  `header_len != 36` consistently across core and bridges (PB-24).
- `run.sh`, both `entrypoint.sh`, `zenohd.sh`, `roscore.sh`, `package-launch-surface.sh`: no tests (shellcheck is not in CI either). A table test of
  `run.sh` (env → emitted `CAM_PIPELINE`, with a stub python) would pin the transport vocabulary (PB-14) and the seam/scale/normalize chain shapes.
- `rig-build.sh`: `RIG_BUILD_NO_CACHE`, `RIG_BUILD_PULL`, `RIG_BUILD_RESULT`, `PUSH=0`, multi-arch refusal are untested (`test_dev_defaults.py`
  already has the fake-docker harness to do it). `check.py` and `build.sh` download/fallback/checksum-failure paths are untested (only the tar
  extractor is). No test asserts the two rig labels on the built images (`docker inspect` in CI on cam-ci would be one line).
- `sensor_env`: no param-less ROS bridge case (PB-8); no instance-name validity case (PB-15).

---

## 5. Checked and found sound

Wire format
- Header layout is identical in all five places: `transport.py:40` (`<4sHHQQHHIBBH`, 36 bytes), `cam_header_bridge.cpp:24-40` and
  `cam_ros1_bridge.cpp:93-109` (packed struct + `static_assert(sizeof == 36)`), `header_transport.py:23`, `ros2-source/framing.py:17`. Field order,
  widths, little-endian (both targets are LE; the C++ relies on that and says so). pixfmt codes 1–15 and ts_source codes 0–4 agree everywhere;
  unknown ts_source degrades safely (`camera` / the number). The existing tests pin code presence and Python byte-equality; they pass here
  (`test_transport.py` 15, `test_ros2_source.py` 5, webrtc helper tests 5/7/8/10/8 + encoder policy — run with `python3.12 -B`).
- Untrusted lengths in the header path are bounded before use: buffer ≥ 36, magic, version, `sizeof <= header_len <= map.size`
  (`cam_header_bridge.cpp:92-108`, `cam_ros1_bridge.cpp:259-274`, `header_transport.py:61-70`), then `size >= width*height*bpp` before the copy
  (`cam_bridge_base.cpp:238-246`, `cam_ros1_bridge.cpp:298-302`); `width*height*bpp` cannot overflow `size_t` (u16×u16×4). The direct (non-YUV)
  copies are safe; the exceptions are PB-25 (YUV, odd sizes) and PB-4 (stride).
- unixfd convention matches on both sides: core sets `offset = frame_id`, `offset_end = capture ns` (`pipeline.py:692-693`); consumers read exactly
  those and guard `GST_BUFFER_OFFSET_NONE` (`cam_unixfd_bridge.cpp:126-129`, `bridge_stream.py:517-529`); caps geometry is validated `> 0`.

C++ lifetime / threading / shutdown
- `GstBuffer` is mapped, copied into the message and unmapped inside one callback (`cam_bridge_base.cpp:208-217`); no pointer into the map escapes
  (`FrameMeta.data` is documented and used as call-scoped; the YUV scratch buffer is touched only by the streaming thread).
- Teardown order is right in both bridges: flag → disconnect `new-sample` → `set_state(NULL)` (joins streaming threads) → join the bus thread; derived
  destructors call `stop_pipeline()` first so no pure-virtual dispatch can happen (`cam_bridge_base.cpp:108-128`, the two `~Cam*Bridge`). The bus
  watcher polls with a timeout and re-checks the flag. Publishing from the streaming thread is safe (rclcpp/roscpp publishers are thread-safe; no
  node state is mutated after construction; parameters are read once).
- SIGTERM/SIGINT: every plugin compose has `init: true`; entrypoints `exec "$@"`; ROS 2 services set `stop_signal: SIGINT` because `ros2 launch`
  ignores SIGTERM (documented at `ros2-bridge/compose.yml:20-26`); `run.sh` `exec`s the launcher so it receives the signal; `bridge_stream.py` handles
  both signals through `GLib.unix_signal_add` and tears down advertiser then pipeline. Shell: `set -e`/`-eu` scripts use `[ … ] && x=…` only in
  non-final list positions, so no `set -e` holes were found; `run.sh`'s unquoted `${PIPELINE}` is deliberate and guarded by `set -f`; the
  `CAM_WEBRTC_MAX_SIZE` parser rejects non-numeric forms before the `-lt` tests.
- Encodings: GRAY8→`mono8`, GRAY16_LE/BE→`mono16` + `is_bigendian`, RGB/BGR/RGBA/BGRA→`rgb8/bgr8/rgba8/bgra8`, 8-bit Bayer patterns→`bayer_<pat>8`
  (unixfd from caps; header from the `CAM_ROS_ENCODING` hint, applied only to 1-byte planes). `step` = `width × bpp`.

Sim time / what clock is stamped
- Nothing reads `RIG_SIM_TIME` or sets `use_sim_time` (confirmed). **Neither ROS bridge stamps from a ROS clock at all**: `header.stamp` is the wire
  value — header `timestamp_ns` (`cam_header_bridge.cpp:136`, `cam_ros1_bridge.cpp:282`) or unixfd `offset_end` (`cam_unixfd_bridge.cpp:129`), which the
  core fills from the frame's capture stamp (`pipeline.py:693,696`). Under orchestrated replay that is whatever the core's replay source emits:
  original capture time when rig renders `replay.retime: original` (its sim-time case, `rigging.yaml:146`), wall-retimed otherwise. So image stamps
  are consistent with `/clock` exactly when the *core* is told to keep original time; the bridge's own clock setting does not affect them.
  What the missing `use_sim_time` does affect: the node clock is used only for `RCLCPP_WARN_THROTTLE` (harmless); the publish-rate throttle uses
  `steady_clock` (`cam_bridge_base.cpp:224-225`), i.e. wall time — at replay speed ≠ 1 a "10 Hz" cap is 10 Hz wall, not stream time; a missing capture
  stamp is published as `0` (1970), never "now" (unixfd `OFFSET_NONE` branch). The WebRTC bridge deliberately paces PTS from arrival
  (`header_transport.PtsTracker`, `bridge_stream.py:311`) and keeps capture ns only as metadata; its latency readout is `time.time_ns() - capture`,
  which is meaningless (and filtered only by a 2001…now+60 s window) for original-time replay — it will print multi-day "latency" for replays.
  `ros2-source` stamps from `msg.header.stamp` (sim-stamped topics stay sim-stamped) and falls back to **wall** `time.time_ns()` for zero stamps
  (PB-7) — the one place a sim-time recording can get wall-clock stamps mixed in.

WebRTC
- Pipeline strings are built from operator-controlled env only (sensor YAML → sensor_env → compose); values are interpolated unquoted into
  gst-launch syntax (`run.sh:178,185,195,223-231`), so a socket path or `VIDEO_CAPS` containing spaces/`!` reshapes the pipeline — a config
  robustness issue, not a privilege boundary (whoever sets env already controls the container).
- Encoder policy: handlers are attached with `connect_after` so the short GOP survives webrtcsink's default handler; only existing properties are
  set; profile is never forced on the element; the forced output caps pin constrained-baseline + the derived level. Level table matches H.264
  Table A-1 for all 17 rows (checked row by row); lowest-fit selection and clamp are correct; `level_covers` warns when the clamp is too low.
- `scale_plan.fit_within`: never upscales, never exceeds the box, even outputs, ≥ 2 (integer floor math checked on both binding axes).
- Core absent / restarts: `run.sh` waits ≤ 60 s then starts; a failed start or a later source error reaches the bus → `had_error` → exit 1 → compose
  restart (basesrc posts "Failed to start" even where the element does not — observed in E1). No wedge equivalent to PB-1 here.
- Per-viewer resources: one capsfilter per consumer handed to webrtcsink; no per-consumer state is retained in Python beyond a counter.
- Zenoh advertiser: queryable declared before the token; failures close the session and are retried on a timer; `close()` on shutdown; the token dies
  with the process. Peer-mode `zenoh.open` does not block on an unreachable router, so the retry path is rarely needed (not executed — no binding here).

Build
- `tools/gstreamer/build.sh`: checksum is verified before extraction and an archive missing from `SHA256SUMS` fails closed (`sha256sum --check` on
  empty input exits 1 — run here); a fallback download never splices bytes from two hosts (`rm -f` first); extraction uses Python's `data` filter
  (`test_gstreamer_archives.py`); meson runs with `--wrap-mode=nofallback -Dauto_features=disabled`.
- `build-images.sh`: variant/tag derivation, image sets per variant (matches `rigging.yaml` and `docs/fleet-build-contract.md:7-11`), the unquoted
  `${VAR:+--build-arg "…"}` expansions (each yields exactly two words), multi-arch + `PUSH=0` refusal (exit 1 — run with the fake docker),
  `PLATFORM_FLAG` suppression under Buildx, `RIG_BUILD_PULL=0` (drops `--pull` only).
- `package-launch-surface.sh`: list extraction ends at the next top-level key, strips comments, verifies every file exists; produced a correct
  30-entry tarball here (minus the files PB-21 says are not listed).
- Dockerfiles: multi-stage, toolchains stay out of runtime layers; `apt` lists removed in the same layer; `--no-upgrade` on the ROS parents as the
  contract describes; labels as stated in section 2.

Experiment index (throwaway scripts, not committed): `e1_stale.py` (E1 stale socket → FAILURE for shmsrc/unixfdsrc; E2 shmsink binds `<path>.0`), `e3_stride.py` (E3/E4),
`e5_ros2source.py` (E5–E7), `e8_bayer16.py` (E8), `e9_advert.sh` (E9), `e10_leak.py` (E10/E11), `e12_yuv.cpp` (E12, ASan), `fakebin/docker`
(rig-build shim), `a.yaml`/`b.yaml` (sensor_env), `dl/Cargo.lock` (gst-plugins-rs 0.15.4 lockfile as fetched).
