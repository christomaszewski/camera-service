#!/usr/bin/env bash
# A single, isolated prefix shared by the dev core and WebRTC images. System plugins (Aravis,
# libnice and gst-plugins-rs) remain available; the media path's C libraries/plugins are 1.28.7.
# No distro packages are overwritten, and no NVIDIA/ROS packages are upgraded.
set -euo pipefail
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
  archive="$module-1.28.7.tar.xz"
  curl --fail --location --retry 3 "https://gstreamer.freedesktop.org/src/$module/$archive" -o "$archive"
  awk -v file="$archive" '$2 == file' /build-gstreamer/SHA256SUMS | sha256sum --check --strict
  tar -xf "$archive"
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
  meson setup "$module-build" "$module-1.28.7" --prefix=/opt/gstreamer --libdir=lib \
    --buildtype=release --wrap-mode=nofallback -Dauto_features=disabled "${options[@]}"
  meson compile -C "$module-build" -j "${GST_BUILD_JOBS:-4}"
  meson install -C "$module-build" --no-rebuild --strip
done
