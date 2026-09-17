#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
webots_bin="/Applications/Webots.app/Contents/MacOS/webots"
world_path="$project_dir/worlds/dream_mode_research.wbt"
outage_mode="${1:-deterministic}"
outage_condition="${2:-configured}"

if [[ ! -x "$webots_bin" ]]; then
  print -u2 "Webots was not found at /Applications/Webots.app"
  exit 1
fi

if [[ "$outage_mode" != "deterministic" && "$outage_mode" != "randomized" ]]; then
  print -u2 "Usage: ./scripts/run_phase2.sh [deterministic|randomized] [configured|black|frozen]"
  exit 2
fi

if [[ "$outage_condition" != "configured" && "$outage_condition" != "black" && "$outage_condition" != "frozen" ]]; then
  print -u2 "Usage: ./scripts/run_phase2.sh [deterministic|randomized] [configured|black|frozen]"
  exit 2
fi

export DREAM_MODE_OUTAGE_MODE="$outage_mode"
if [[ "$outage_condition" == "configured" ]]; then
  unset DREAM_MODE_OUTAGE_CONDITION
else
  export DREAM_MODE_OUTAGE_CONDITION="$outage_condition"
fi

PYTHONDONTWRITEBYTECODE=1 exec "$webots_bin" "$world_path"
