#!/usr/bin/env bash
# Package JUST the launch surface -- gige-up + the compose fragments + sensor_env + rigging.yaml --
# into a tarball you drop on a vehicle. No source tree, no Dockerfiles, no tests: the vehicle PULLS
# images from your registry and only needs these files to run gige-up. The file list IS the rig
# descriptor's `launch_surface` (the single source of truth), so the bundle can't drift from it.
#
#   tools/package-launch-surface.sh [output.tar.gz]        (default: dist/gige-vision-launch.tar.gz)
#
# On the vehicle:
#   mkdir -p ~/gige && tar -xzf gige-vision-launch.tar.gz -C ~/gige && cd ~/gige
#   export GIGE_REGISTRY=<registry:5000>
#   ./gige-up /path/to/sensor.yaml pull && ./gige-up /path/to/sensor.yaml up -d
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
OUT="${1:-dist/gige-vision-launch.tar.gz}"

# The rig descriptor: prefer rigging.yaml, fall back to the legacy deploy.yaml name (rig does the same).
DESC="rigging.yaml"; [ -f "$DESC" ] || DESC="deploy.yaml"
[ -f "$DESC" ] || { echo "package: no rigging.yaml (or deploy.yaml) rig descriptor in $REPO" >&2; exit 1; }

# Extract the launch_surface list from the descriptor (awk, so the build host needs no PyYAML).
FILES="$(awk '
  /^launch_surface:/ {ls=1; next}
  ls && /^[A-Za-z]/  {ls=0}                         # next top-level key ends the list
  ls && /^[[:space:]]*-[[:space:]]/ {
    sub(/#.*/, ""); sub(/^[[:space:]]*-[[:space:]]*/, ""); sub(/[[:space:]]+$/, "")
    if (length) print
  }
' "$DESC")"
[ -n "$FILES" ] || { echo "package: no launch_surface found in $DESC" >&2; exit 1; }

missing=0
for f in $FILES; do [ -e "$f" ] || { echo "package: listed file missing: $f" >&2; missing=1; }; done
[ "$missing" = 0 ] || { echo "package: fix $DESC launch_surface and retry" >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
tar -czf "$OUT" "$DESC" $FILES                      # repo-relative paths preserve the layout on extract
echo "packaged $(printf '%s\n' $FILES | wc -l | tr -d ' ') launch-surface files + $DESC -> $OUT"
tar -tzf "$OUT" | sed 's/^/  /'
echo
echo "Drop on a vehicle (no source clone):"
echo "  scp $OUT <vehicle>:~ ; ssh <vehicle>"
echo "  mkdir -p ~/gige && tar -xzf $(basename "$OUT") -C ~/gige && cd ~/gige"
echo "  export GIGE_REGISTRY=<registry:5000>"
echo "  ./gige-up /path/to/sensor.yaml pull && ./gige-up /path/to/sensor.yaml up -d"
