#!/usr/bin/env bash
# Save the jp6m image set as one tarball for a Jetson without registry access (docs/jp6-modern-userspace.md).
#   tools/jp6-modern/export-images.sh [DIR]      -> DIR/jp6m-images.tar (default ./jp6m-images)
# On the Jetson:  docker load < jp6m-images.tar   (then tools/jp6-modern/cam-up-jp6m ... up -d)
set -eu
DIR="${1:-./jp6m-images}"
TAG="${JP6M_TAG:-jp6m}"
mkdir -p "$DIR"
IMAGES=()
for img in cam-core webrtc-bridge ros2-bridge; do
  if docker image inspect "$img:$TAG" >/dev/null 2>&1; then IMAGES+=("$img:$TAG"); else echo "export: $img:$TAG not built -- skipping" >&2; fi
done
[ ${#IMAGES[@]} -gt 0 ] || { echo "export: nothing to save (build with RIG_TARGET_PLATFORM=jp6m tools/build-images.sh, PUSH=0)" >&2; exit 1; }
{
  echo "# jp6m image set, $(date -u +%Y-%m-%dT%H:%M:%SZ), $(uname -m)"
  for i in "${IMAGES[@]}"; do echo "$i $(docker image inspect "$i" --format '{{.Id}} {{.Size}}')"; done
} > "$DIR/MANIFEST.txt"
echo "saving ${IMAGES[*]} -> $DIR/jp6m-images.tar" >&2
docker save "${IMAGES[@]}" -o "$DIR/jp6m-images.tar"
ls -lh "$DIR/jp6m-images.tar" | awk '{print "  " $5 "  " $9}'
cat "$DIR/MANIFEST.txt"
