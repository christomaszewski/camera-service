#!/usr/bin/env bash
# jp6m probe -- run ON the JetPack 6 host (docs/jp6-modern-userspace.md, step 2).
#
# Question: does a modern userspace container (Ubuntu 26.04 / GStreamer 1.28) SEE and USE the JP6
# host's NVIDIA multimedia stack -- the nvv4l2* / nvvidconv plugins the host built against
# GStreamer 1.20 -- when the host injects it? For every injection MODE x IMAGE it records:
#   host facts, the mounted stack, unresolved libraries, whether each nv plugin LOADS (and the
#   registry's reason when not), the element table, encode throughput for the recorder's lossless
#   HEVC path and a 1080p H.264 stream (with x264 as the software baseline), a decode round trip,
#   and the bit-exact NVENC test (core images). Everything lands in --out (default ./jp6m-results),
#   one log per run plus summary.txt -- attach the directory to the write-up.
#
#   tools/jp6-modern/probe.sh                          # modes csv,cdi x images cam-core:jp6m,cam-core:jp6,webrtc-bridge:jp6m
#   tools/jp6-modern/probe.sh --modes csv --images cam-core:jp6m
#   tools/jp6-modern/probe.sh --frames 120             # shorter benchmarks
#   tools/jp6-modern/probe.sh --hostlibs --images cam-core:jp6m   # the host's multimedia stack mounted in
#
# csv = `--runtime nvidia` (the JP6 CSV mounts);  cdi = `--device nvidia.com/gpu=all` (needs
# /etc/cdi/nvidia.yaml from `sudo nvidia-ctk cdi generate --mode=csv`);  hostlibs = csv PLUS the
# host's own multimedia userspace bind-mounted in (JetPack 6's runtime injects drivers + devices
# only -- drivers.csv/devices.csv -- and leaves the multimedia + GStreamer plugin layer to the
# container; this mode answers "do the host's 1.20-built nv plugins run inside GStreamer 1.28"
# without rebuilding an image). Images that are not present locally are skipped with a note.
# Only hostlibs mounts anything from the host (read-only, under /opt/hostnv); nothing is written
# outside --out.
set -u
MODES="csv,cdi"
IMAGES="cam-core:jp6m,cam-core:jp6,webrtc-bridge:jp6m"
OUT="./jp6m-results"
FRAMES=300
while [ $# -gt 0 ]; do
  case "$1" in
    --modes) MODES="$2"; shift 2 ;;
    --hostlibs) MODES="hostlibs" ; shift ;;
    --images) IMAGES="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --frames) FRAMES="$2"; shift 2 ;;
    -h|--help) sed -n 2,22p "$0"; exit 0 ;;
    *) echo "probe: unknown arg $1" >&2; exit 2 ;;
  esac
done
mkdir -p "$OUT"
SUMMARY="$OUT/summary.txt"
: > "$SUMMARY"
say() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }

say "# jp6m probe -- $(date -u +%Y-%m-%dT%H:%M:%SZ) on $(hostname)"
say ""
say "## host"
{
  echo "nv_tegra_release: $(cat /etc/nv_tegra_release 2>/dev/null | head -1 || echo '(absent -- not a Jetson?)')"
  echo "kernel: $(uname -r)"
  echo "docker: $(docker --version 2>/dev/null)"
  echo "nvidia-ctk: $(nvidia-ctk --version 2>/dev/null | head -1 || echo '(absent)')"
  echo "nvidia-container-cli: $(nvidia-container-cli --version 2>/dev/null | head -1 || echo '(absent)')"
  echo "runtimes: $(docker info 2>/dev/null | sed -n 's/^ *Runtimes: //p')"
  echo "csv files: $(ls /etc/nvidia-container-runtime/host-files-for-container.d/ 2>/dev/null | tr '\n' ' ' || true)"
  echo "cdi spec: $([ -s /etc/cdi/nvidia.yaml ] && echo "/etc/cdi/nvidia.yaml ($(grep -c 'hostPath' /etc/cdi/nvidia.yaml) mounts)" || echo 'absent -- cdi mode will fail; sudo nvidia-ctk cdi generate --mode=csv --output=/etc/cdi/nvidia.yaml')"
  echo "host gstreamer: $(gst-inspect-1.0 --version 2>/dev/null | head -1 || echo '(no gst-inspect on host)')"
  echo "host nvv4l2h264enc: $(gst-inspect-1.0 nvv4l2h264enc >/dev/null 2>&1 && echo present || echo absent)"
  echo "host nv plugins: $(ls /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstnv*.so 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
  echo "host nvidia lib dir: $(ls -d /usr/lib/aarch64-linux-gnu/nvidia 2>/dev/null || ls -d /usr/lib/aarch64-linux-gnu/tegra 2>/dev/null || echo none) ($(ls /usr/lib/aarch64-linux-gnu/nvidia /usr/lib/aarch64-linux-gnu/tegra 2>/dev/null | wc -l) files)"
  echo "host libv4l2: $(ls /usr/lib/aarch64-linux-gnu/libv4l2.so* 2>/dev/null | tr '\n' ' ')  nv v4l plugins: $(ls /usr/lib/aarch64-linux-gnu/libv4l/plugins/nv 2>/dev/null | tr '\n' ' ')"
  echo "csv gst/multimedia lines: $(cat /etc/nvidia-container-runtime/host-files-for-container.d/*.csv 2>/dev/null | grep -ciE 'gstreamer|nvbufsurface|nvv4l2|libv4l')"
  present=""; missing=""
  for d in /dev/v4l2-nvenc /dev/v4l2-nvdec /dev/v4l2-nvjpg /dev/nvhost-msenc /dev/nvhost-nvdec /dev/nvhost-vic /dev/nvmap; do
    [ -e "$d" ] || continue
    if cat /etc/nvidia-container-runtime/host-files-for-container.d/*.csv 2>/dev/null | awk -F'[, ]+' '/^dev/{print $2}' | grep -qx "$d"; then present="$present $d"; else missing="$missing $d"; fi
  done
  echo "codec device nodes in devices.csv:${present:- none}"
  echo "codec device nodes the host has but the csv lacks (the probe grants these itself):${missing:- none}"
} | tee -a "$SUMMARY"

# The v4l2 codec device nodes. JetPack 6's devices.csv (nvidia-container-toolkit 1.16) lists the
# nvhost-* nodes but NOT /dev/v4l2-nvenc / -nvdec / -nvjpg, which the r36 plugins open -- so
# nvv4l2h26xenc fails with "Cannot identify device '/dev/v4l2-nvenc'" in every injection mode.
# Every mode grants the ones the host has and the CSV lists nowhere (a csv-mode CDI spec inherits
# the same gap). The stack's fix is docker-compose.jp6.yml.
NV_DEVICES=(/dev/v4l2-nvenc /dev/v4l2-nvdec /dev/v4l2-nvjpg /dev/nvhost-msenc /dev/nvhost-nvdec /dev/nvhost-nvjpg /dev/nvhost-vic /dev/nvmap)
csv_devices() { cat /etc/nvidia-container-runtime/host-files-for-container.d/*.csv 2>/dev/null | awk -F'[, ]+' '/^dev/{print $2}'; }
device_args() {
  local covered a=()
  covered="$(csv_devices)"
  for d in "${NV_DEVICES[@]}"; do
    [ -e "$d" ] || continue
    printf '%s\n' "$covered" | grep -qx "$d" && continue
    a+=(--device "$d")
  done
  [ ${#a[@]} -gt 0 ] && printf '%s\n' "${a[@]}"
  return 0
}

# hostlibs: the host's multimedia userspace, read-only, at paths that cannot collide with what the
# runtime injects (the runtime bind-mounts single files into /usr/lib/aarch64-linux-gnu/nvidia --
# a directory mounted read-only there would make those mounts fail). The in-container probe puts
# /opt/hostnv on LD_LIBRARY_PATH / GST_PLUGIN_PATH when JP6M_HOSTLIBS=1.
hostlibs_args() {
  local nvdir="" a=()
  for d in /usr/lib/aarch64-linux-gnu/nvidia /usr/lib/aarch64-linux-gnu/tegra; do [ -d "$d" ] && { nvdir="$d"; break; }; done
  [ -n "$nvdir" ] && a+=(-v "$nvdir:/opt/hostnv/nvidia:ro")
  for p in nvvidconv nvvideo4linux2 nvjpeg nvunixfd nvtee nvcompositor; do
    f="/usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgst$p.so"; [ -e "$f" ] && a+=(-v "$f:/opt/hostnv/gst/libgst$p.so:ro")
  done
  # the v4l2 codec plugin: on r36 the host's libv4l/plugins/nv/ entries are RELATIVE symlinks into
  # nvidia/, which would dangle inside the container -- bind the real file where libv4l2 dlopens it.
  # NVIDIA's own libv4l2 (nvidia/libv4l2.so.0 -> libnvv4l2.so) rides in the nvidia dir mount and wins
  # through LD_LIBRARY_PATH.
  for pl in libv4l2_nvvideocodec.so; do
    f="$nvdir/$pl"; [ -e "$f" ] && a+=(-v "$f:/usr/lib/aarch64-linux-gnu/libv4l/plugins/nv/$pl:ro")
  done
  a+=(-e JP6M_HOSTLIBS=1)
  printf '%s\n' "${a[@]}"
}
say ""

# The in-container probe. Bash, no python: the core image's python is fine but the webrtc image may differ.
read -r -d '' PROBE <<'INNER' || true
set -u
FRAMES="$1"; KIND="$2"
line() { printf '%s\n' "$*"; }
if [ "${JP6M_HOSTLIBS:-}" = 1 ]; then
  export LD_LIBRARY_PATH="/opt/hostnv/nvidia:${LD_LIBRARY_PATH:-}"
  export GST_PLUGIN_PATH="/opt/hostnv/gst:${GST_PLUGIN_PATH:-}"
  line "hostlibs: nvidia libs $(ls /opt/hostnv/nvidia 2>/dev/null | wc -l), gst plugins $(ls /opt/hostnv/gst 2>/dev/null | tr '\n' ' '), v4l plugin $(ls /usr/lib/aarch64-linux-gnu/libv4l/plugins/nv 2>/dev/null | tr '\n' ' '), libv4l2 -> $(readlink -f "$(ldconfig -p 2>/dev/null | grep -m1 'libv4l2.so.0 ' | awk '{print \$NF}')" 2>/dev/null)"
fi
line "os: $(. /etc/os-release; echo "$PRETTY_NAME") glibc $(ldd --version | head -1 | awk '{print $NF}')"
line "gstreamer: $(gst-inspect-1.0 --version | head -1)"
line "nvidia dir (r36): $(ls /usr/lib/aarch64-linux-gnu/nvidia 2>/dev/null | wc -l) libs; tegra dir (r35): $(ls /usr/lib/aarch64-linux-gnu/tegra 2>/dev/null | wc -l) libs; ld.so.conf lists them: $(grep -rsE 'nvidia|tegra' /etc/ld.so.conf.d/ >/dev/null && echo yes || echo NO)"
line "ldcache has nvbufsurface: $(ldconfig -p 2>/dev/null | grep -c nvbufsurface)"
line "runtime asked (NVIDIA_VISIBLE_DEVICES): ${NVIDIA_VISIBLE_DEVICES:-UNSET -- the CSV runtime injects nothing}"
line "devices: $(ls /dev/nvhost-msenc /dev/nvhost-nvdec /dev/nvhost-vic /dev/v4l2-nvenc /dev/v4l2-nvdec /dev/nvmap 2>/dev/null | tr '\n' ' ')"
line "nv plugins in container: $(ls /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstnv*.so /opt/hostnv/gst/libgstnv*.so 2>/dev/null | xargs -n1 basename 2>/dev/null | tr '\n' ' ')"
line ""
line "## unresolved libraries (ldd) per nv plugin"
for p in /usr/lib/aarch64-linux-gnu/gstreamer-1.0/libgstnv*.so /opt/hostnv/gst/libgstnv*.so; do
  [ -e "$p" ] || { line "(no nv plugins mounted)"; break; }
  miss="$(ldd "$p" 2>&1 | grep -E 'not found' | awk '{print $1}' | tr '\n' ' ')"
  line "$(basename "$p"): ${miss:-ok}"
done
line ""
line "## plugin load (registry reasons)"
GST_DEBUG=GST_PLUGIN_LOADING:4,GST_REGISTRY:4 gst-inspect-1.0 nvv4l2h264enc 2>&1 | grep -iE 'libgstnv|blacklist|failed|error|not found|undefined' | sed 's/^/  /' | head -12
line ""
line "## elements"
for e in nvvidconv nvv4l2h264enc nvv4l2h265enc nvv4l2decoder nvjpegenc nvjpegdec nvarguscamerasrc unixfdsink unixfdsrc aravissrc webrtcsink webrtcsrc rtpgccbwe x264enc x265enc avdec_h264; do
  if gst-inspect-1.0 "$e" >/dev/null 2>&1; then line "  $e: yes"; else line "  $e: no"; fi
done
line ""
bench() {  # bench <label> <pipeline...>
  local label="$1"; shift
  local t0 t1 fps rc
  t0=$(date +%s.%N)
  if out="$(timeout 120 gst-launch-1.0 -q "$@" 2>&1)"; then rc=0; else rc=$?; fi
  t1=$(date +%s.%N)
  if [ $rc -eq 0 ]; then
    fps=$(awk -v n="$FRAMES" -v a="$t0" -v b="$t1" 'BEGIN{printf "%.1f", n/(b-a)}')
    line "  $label: $FRAMES frames in $(awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.2f", b-a}') s = $fps fps"
  else
    line "  $label: FAILED (rc=$rc): $(printf '%s' "$out" | grep -iE 'error|not negotiated|could not|no element|erroneous' | head -2 | tr '\n' ' ')"
  fi
}
line "## encode throughput ($FRAMES frames each, videotestsrc, wall clock incl. startup)"
bench "h264 1080p NV12 nvv4l2h264enc" videotestsrc num-buffers="$FRAMES" pattern=smpte ! video/x-raw,format=NV12,width=1920,height=1080,framerate=60/1 ! nvvidconv ! 'video/x-raw(memory:NVMM),format=NV12' ! nvv4l2h264enc ! h264parse ! fakesink sync=false
bench "hevc lossless 2048x1536 GRAY8->NV24 nvv4l2h265enc (recorder path)" videotestsrc num-buffers="$FRAMES" pattern=snow ! video/x-raw,format=GRAY8,width=2048,height=1536,framerate=30/1 ! videoconvert ! video/x-raw,format=NV24 ! nvvidconv ! 'video/x-raw(memory:NVMM),format=NV24' ! nvv4l2h265enc enable-lossless=1 maxperf-enable=1 ! h265parse ! fakesink sync=false
bench "h264 1080p encode + nvv4l2decoder round trip" videotestsrc num-buffers="$FRAMES" pattern=smpte ! video/x-raw,format=NV12,width=1920,height=1080,framerate=60/1 ! nvvidconv ! 'video/x-raw(memory:NVMM),format=NV12' ! nvv4l2h264enc ! h264parse ! nvv4l2decoder ! fakesink sync=false
bench "h264 1080p x264enc ultrafast (software baseline)" videotestsrc num-buffers="$FRAMES" pattern=smpte ! video/x-raw,format=I420,width=1920,height=1080,framerate=60/1 ! x264enc speed-preset=ultrafast tune=zerolatency ! fakesink sync=false
line ""
if [ "$KIND" = core ] && [ -f /app/tools/nvenc_lossless_test.py ]; then
  line "## bit-exact NVENC lossless (tools/nvenc_lossless_test.py, 30 frames)"
  (cd /app && timeout 300 python3 tools/nvenc_lossless_test.py --frames 30 2>&1 | tail -4 | sed 's/^/  /')
fi
if [ "$KIND" = webrtc ]; then
  line "## webrtcsink"
  gst-inspect-1.0 webrtcsink 2>/dev/null | grep -E '^  (Version|Rank|Description)' | sed 's/^/  /'
  line "  properties: $(gst-inspect-1.0 webrtcsink 2>/dev/null | grep -cE '^  [a-z-]+ +:')"
  line "  encoders webrtcsink can rank: $(for e in nvv4l2h264enc nvv4l2vp8enc nvv4l2vp9enc nvv4l2av1enc x264enc vp8enc av1enc; do gst-inspect-1.0 $e >/dev/null 2>&1 && printf '%s ' $e; done)"
fi
INNER

for mode in ${MODES//,/ }; do
  case "$mode" in
    # the CSV-mode runtime injects only into a container that asks (NVIDIA_VISIBLE_DEVICES); a plain
    # Ubuntu image does not ask by itself, so every runtime mode says it here
    csv) RT=(--runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all) ;;
    cdi) RT=(--device nvidia.com/gpu=all) ;;
    hostlibs) RT=(--runtime nvidia -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all); while IFS= read -r x; do RT+=("$x"); done < <(hostlibs_args) ;;
    none) RT=() ;;
  esac
  case "$mode" in
    csv|cdi|hostlibs) while IFS= read -r x; do [ -n "$x" ] && RT+=("$x"); done < <(device_args) ;;
    *) say "!! unknown mode $mode (csv|cdi|hostlibs|none)"; continue ;;
  esac
  for img in ${IMAGES//,/ }; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then say "## $mode x $img: image not present locally -- skipped"; say ""; continue; fi
    kind=core; case "$img" in webrtc*) kind=webrtc ;; esac
    log="$OUT/${mode}-$(printf '%s' "$img" | tr '/:' '__').log"
    say "## $mode x $img  (log: $log)"
    docker run --rm -i ${RT[@]+"${RT[@]}"} --entrypoint bash "$img" -s "$FRAMES" "$kind" <<<"$PROBE" > "$log" 2>&1
    rc=$?
    [ $rc -ne 0 ] && say "  (container exited $rc)"
    # the verdict lines
    grep -E '^gstreamer:|^hostlibs:|^nv plugins in container|^  nvv4l2h264enc: |^  nvv4l2h265enc: |^  nvvidconv: |^  unixfdsink: |^  webrtcsink: | fps$|FAILED|LOSSLESS|Version' "$log" | sed 's/^/  /' | tee -a "$SUMMARY"
    say ""
  done
done
say "done: $SUMMARY"
