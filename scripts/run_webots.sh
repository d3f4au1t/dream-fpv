#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
webots_bin="/Applications/Webots.app/Contents/MacOS/webots"
world_path="$project_dir/worlds/dream_mode_research.wbt"

if [[ ! -x "$webots_bin" ]]; then
  print -u2 "Webots was not found at /Applications/Webots.app"
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 exec "$webots_bin" "$world_path"
