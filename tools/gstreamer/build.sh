#!/usr/bin/env bash
# A single, isolated prefix shared by the dev core and WebRTC images. System plugins (Aravis,
# libnice and gst-plugins-rs) remain available; the media path's C libraries/plugins are 1.28.8.
# No distro packages are overwritten, and no NVIDIA/ROS packages are upgraded.
set -euo pipefail

download_archive() {
  local download_url="$1" archive_path="$2"
  local download_deadline=$((SECONDS + ${3:-120})) retry_delay=1
  while true; do
    # Restart curl to re-read the partial file's size after every interrupted transfer.
    # HTTP/1.1 avoids the release CDN's intermittent HTTP/2 stream resets.
    if curl --http1.1 --fail --location --continue-at - --connect-timeout 15 --max-time 60 \
      "$download_url" -o "$archive_path"; then
      return 0
    fi
    (( SECONDS < download_deadline )) || return 1
    sleep "$retry_delay"
    (( retry_delay >= 16 )) || retry_delay=$((retry_delay * 2))
  done
}

extract_archive() {
  # GNU tar in Ubuntu 26.04 requires openat2 during extraction; some CPU emulators
  # do not implement it. Python's data filter supports those builders and rejects
  # archive paths/links outside the extraction directory. Checksums are verified first.
  python3 -m tarfile --filter data -e "$1"
}

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl xz-utils build-essential pkg-config meson ninja-build flex bison \
  python3 python3-gi gobject-introspection libgirepository1.0-dev libglib2.0-dev \
  liborc-0.4-dev libunwind-dev libdw-dev libjpeg-dev libpng-dev libvpx-dev libx264-dev \
  libavcodec-dev libavformat-dev libavfilter-dev libswscale-dev libavutil-dev \
  libnice-dev libssl-dev libsrtp2-dev libpango1.0-dev libopus-dev libgudev-1.0-dev \
  libv4l-dev zlib1g-dev

export PATH=/opt/gstreamer/bin:$PATH
export LD_LIBRARY_PATH=/opt/gstreamer/lib
export PKG_CONFIG_PATH=/opt/gstreamer/lib/pkgconfig
export GI_TYPELIB_PATH=/opt/gstreamer/lib/girepository-1.0
mkdir -p /tmp/gstreamer-src
cd /tmp/gstreamer-src
for module in gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav gst-rtsp-server; do
  archive="$module-1.28.8.tar.xz"
  if ! download_archive "https://gstreamer.freedesktop.org/src/$module/$archive" "$archive" 30; then
    # Debian mirrors the unchanged upstream release archives. Never splice bytes from two hosts;
    # the same pinned upstream SHA-256 below must pass before either download is extracted.
    rm -f "$archive"
    download_archive "https://deb.debian.org/debian/pool/main/g/${module}1.0/${module}1.0_1.28.8.orig.tar.xz" "$archive"
  fi
  awk -v file="$archive" '$2 == file' /build-gstreamer/SHA256SUMS | sha256sum --check --strict
  extract_archive "$archive"
  options=()
  case "$module" in
    gstreamer) features="introspection tools coretracers libunwind libdw" ;;
    gst-plugins-base) features="introspection orc app audioconvert audioresample audiotestsrc compositor gio playback rawparse tcp typefind videoconvertscale videorate videotestsrc volume opus pango" ;;
    gst-plugins-good) features="orc autodetect avi imagefreeze isomp4 matroska multifile multipart rtp rtpmanager rtsp udp videobox videocrop videofilter jpeg png vpx v4l2" ;;
    gst-plugins-bad) features="introspection orc bayer debugutils jpegformat unixfd videoparsers dtls sctp shm srtp webrtc" ;;
    gst-plugins-ugly) features="orc x264 gpl" ;;
    gst-libav) features="" ;;
    gst-rtsp-server) features="introspection rtspclientsink" ;;
  esac
  for feature in $features; do options+=("-D$feature=enabled"); done
  meson setup "$module-build" "$module-1.28.8" --prefix=/opt/gstreamer --libdir=lib \
    --buildtype=release --wrap-mode=nofallback -Dauto_features=disabled "${options[@]}"
  meson compile -C "$module-build" -j "${GST_BUILD_JOBS:-4}"
  meson install -C "$module-build" --no-rebuild --strip
done
