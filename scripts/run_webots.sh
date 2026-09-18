#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
webots_bin="/Applications/Webots.app/Contents/MacOS/webots"
world_path="$project_dir/worlds/dream_mode_research.wbt"
video_style="${1:-digital}"

if [[ ! -x "$webots_bin" ]]; then
  print -u2 "Webots was not found at /Applications/Webots.app"
  exit 1
fi

if [[ "$video_style" != "digital" && "$video_style" != "analog" ]]; then
  print -u2 "Usage: ./scripts/run_webots.sh [digital|analog]"
  exit 2
fi

export DREAM_MODE_VIDEO_STYLE="$video_style"
/usr/bin/defaults write com.cyberbotics.Webots-R2025a View3d.hideAllCameraOverlays -bool true
PYTHONDONTWRITEBYTECODE=1 exec "$webots_bin" "$world_path"
