#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
webots_app="/Applications/Webots.app"
webots_bin="/Applications/Webots.app/Contents/MacOS/webots"
world_path="$project_dir/worlds/dream_mode_research.wbt"
smoke_dir="$(mktemp -d /tmp/dream-mode-webots-smoke.XXXXXX)"
output_file=""
error_file=""
run_dir=""
webots_pid=""
port=""
keep_smoke="${DREAM_MODE_KEEP_SMOKE:-0}"
unset DREAM_MODE_KEEP_SMOKE
unset DREAM_MODE_OUTAGE_MODE DREAM_MODE_OUTAGE_CONDITION
unset DREAM_MODE_OUTAGE_SEED DREAM_MODE_OUTAGE_SCRIPT
unset DREAM_MODE_LOG_DISPLAY_FRAMES

terminate_test_instance() {
  if [[ -n "$webots_pid" ]] && kill -0 "$webots_pid" 2>/dev/null; then
    kill "$webots_pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$webots_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL "$webots_pid" 2>/dev/null || true
    wait "$webots_pid" 2>/dev/null || true
  fi
  if [[ -n "$port" ]]; then
    instance_pids=(
      "${(@f)$(pgrep -f "^${webots_bin} .*--port=${port}( |$)" 2>/dev/null || true)}"
    )
    for instance_pid in "${instance_pids[@]}"; do
      [[ -n "$instance_pid" ]] && kill "$instance_pid" 2>/dev/null || true
    done
    for _ in {1..20}; do
      pgrep -f "^${webots_bin} .*--port=${port}( |$)" >/dev/null 2>&1 || break
      sleep 0.1
    done
    instance_pids=(
      "${(@f)$(pgrep -f "^${webots_bin} .*--port=${port}( |$)" 2>/dev/null || true)}"
    )
    for instance_pid in "${instance_pids[@]}"; do
      [[ -n "$instance_pid" ]] && kill -KILL "$instance_pid" 2>/dev/null || true
    done
  fi
}

cleanup() {
  terminate_test_instance
  if [[ "$keep_smoke" == "1" ]]; then
    print "Preserved smoke-test artifacts: $smoke_dir"
  else
    rm -rf "$smoke_dir"
  fi
}
trap cleanup EXIT

if [[ ! -x "$webots_bin" ]]; then
  print -u2 "Webots was not found at /Applications/Webots.app"
  exit 1
fi

available_port() {
  python3 - <<'PY'
import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
}

run_smoke_attempt() {
  local attempt="$1"
  local attempt_dir="$smoke_dir/attempt_$attempt"
  mkdir -p "$attempt_dir"
  output_file="$attempt_dir/webots-output.txt"
  error_file="$attempt_dir/webots-error.txt"
  run_dir="$attempt_dir/log"
  port="$(available_port)"

  if ! /usr/bin/open -F -g -j -n -a "$webots_app" \
    -o "$output_file" --stderr "$error_file" \
    --env QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM=1 \
    --env DREAM_MODE_SMOKE_STEPS=800 \
    --env DREAM_MODE_FIXED_ROLL=0.0 \
    --env DREAM_MODE_FIXED_PITCH=0.0 \
    --env DREAM_MODE_FIXED_YAW=0.0 \
    --env DREAM_MODE_FIXED_THROTTLE=0.36 \
    --env DREAM_MODE_RATE_IMPULSE_STEP=300 \
    --env DREAM_MODE_INITIAL_ROLL_RATE=1.0 \
    --env DREAM_MODE_INITIAL_PITCH_RATE=1.0 \
    --env DREAM_MODE_INITIAL_YAW_RATE=1.0 \
    --env DREAM_MODE_BYPASS_ARMING=1 \
    --env DREAM_MODE_DISABLE_JOYSTICK=1 \
    --env DREAM_MODE_LOG_DIR="$run_dir" \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --args --batch --no-rendering --mode=fast \
    --port="$port" --stdout --stderr "$world_path"; then
    return 1
  fi

  local deadline=$((SECONDS + 45))
  local seen_instance=0
  while [[ ! -s "$run_dir/smoke_test_ok.json" ]]; do
    if pgrep -f "^${webots_bin} .*--port=${port}( |$)" >/dev/null 2>&1; then
      seen_instance=1
    elif (( seen_instance )); then
      terminate_test_instance
      return 1
    fi
    if (( SECONDS >= deadline )); then
      terminate_test_instance
      return 1
    fi
    sleep 0.2
  done

  for _ in {1..50}; do
    pgrep -f "^${webots_bin} .*--port=${port}( |$)" >/dev/null 2>&1 || break
    sleep 0.1
  done
  terminate_test_instance
  return 0
}

smoke_complete=0
for attempt in 1 2 3; do
  if run_smoke_attempt "$attempt"; then
    smoke_complete=1
    break
  fi
  if (( attempt < 3 )); then
    print "Smoke-test startup attempt $attempt failed; retrying."
    sleep "$attempt"
  fi
done

if (( ! smoke_complete )); then
  cat "$output_file" "$error_file"
  print -u2 "Webots smoke test failed to start after 3 attempts"
  exit 1
fi

required_files=(
  run_manifest.json
  telemetry.csv
  rgb_initial.png
  depth_initial.png
  depth_initial.f32
  depth_initial.json
  outage_schedule.json
  outage_events.jsonl
  display_frames.csv
)
for required_file in "${required_files[@]}"; do
  if [[ ! -s "$run_dir/$required_file" ]]; then
    cat "$output_file" "$error_file"
    print -u2 "Missing or empty smoke-test artifact: $required_file"
    exit 1
  fi
done

row_count="$(wc -l < "$run_dir/telemetry.csv" | tr -d ' ')"
if (( row_count < 10 )); then
  cat "$output_file" "$error_file"
  print -u2 "Telemetry contains too few rows: $row_count"
  exit 1
fi

if ! python3 - \
  "$run_dir/smoke_test_ok.json" \
  "$project_dir/config/controller.json" \
  "$run_dir/run_manifest.json" \
  "$run_dir/rgb_initial.png" \
  "$run_dir/depth_initial.png" \
  "$run_dir/depth_initial.f32" \
  "$run_dir/depth_initial.json" \
  "$run_dir/telemetry.csv" \
  "$run_dir/outage_schedule.json" \
  "$run_dir/outage_events.jsonl" \
  "$run_dir/display_frames.csv" <<'PY'
import csv
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

marker_path, config_path, run_manifest_path, rgb_path, depth_png_path, depth_raw_path, depth_metadata_path, telemetry_path, outage_schedule_path, outage_events_path, display_frames_path = map(
    Path, sys.argv[1:]
)

with marker_path.open(encoding="utf-8") as marker_file:
    marker = json.load(marker_file)
with config_path.open(encoding="utf-8") as config_file:
    controller_config = json.load(config_file)
    flight_profile = controller_config["flight_profile"]
with (config_path.parent.parent / controller_config["apparatus_manifest"]).open(
    encoding="utf-8"
) as apparatus_file:
    selected_apparatus = json.load(apparatus_file)
with run_manifest_path.open(encoding="utf-8") as manifest_file:
    run_manifest = json.load(manifest_file)

apparatus = run_manifest.get("apparatus", {})
if (
    apparatus.get("id") != selected_apparatus.get("apparatus_id")
    or apparatus.get("version") != selected_apparatus.get("version")
):
    raise SystemExit(f"Unexpected apparatus identity: {apparatus}")
if marker.get("apparatus_id") != apparatus["id"] or marker.get("apparatus_version") != apparatus["version"]:
    raise SystemExit("Smoke marker and run manifest apparatus identity disagree")
fingerprint = run_manifest.get("run_fingerprint_sha256", "")
if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
    raise SystemExit("Run manifest has an invalid fingerprint")
if marker.get("run_fingerprint_sha256") != fingerprint:
    raise SystemExit("Smoke marker and run manifest fingerprints disagree")
controller_config_sha256 = run_manifest.get("controller_config_sha256", "")
if (
    len(controller_config_sha256) != 64
    or any(character not in "0123456789abcdef" for character in controller_config_sha256)
):
    raise SystemExit("Run manifest has an invalid controller config SHA-256")
if hashlib.sha256(config_path.read_bytes()).hexdigest() != controller_config_sha256:
    raise SystemExit("Run manifest controller config SHA-256 does not match controller.json")
locked_config_sha256 = apparatus.get("locked_file_hashes", {}).get(
    "config/controller.json"
)
if locked_config_sha256 != controller_config_sha256:
    raise SystemExit("Explicit and frozen controller config SHA-256 values disagree")
if marker.get("controller_config_sha256") != controller_config_sha256:
    raise SystemExit("Smoke marker and run manifest controller config SHA-256 disagree")
if marker.get("pilot_display_width") != 480 or marker.get("pilot_display_height") != 270:
    raise SystemExit("Smoke marker has unexpected pilot display dimensions")
if marker.get("outage_mode") != "off":
    raise SystemExit("Normal smoke run unexpectedly enabled outage injection")
runtime = run_manifest.get("runtime", {})
if runtime.get("webots_actual_version") != runtime.get("webots_tested_version"):
    raise SystemExit(f"Unexpected Webots runtime version: {runtime}")

expected_rgb = (480, 270)
expected_depth = (320, 180)
if (marker["rgb_width"], marker["rgb_height"]) != expected_rgb:
    raise SystemExit(f"Unexpected RGB device dimensions: {marker}")
if (marker["depth_width"], marker["depth_height"]) != expected_depth:
    raise SystemExit(f"Unexpected depth device dimensions: {marker}")


def png_dimensions(path):
    with path.open("rb") as image_file:
        header = image_file.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise SystemExit(f"Invalid PNG image: {path.name}")
    return struct.unpack(">II", header[16:24])


if png_dimensions(rgb_path) != expected_rgb:
    raise SystemExit("Saved RGB image dimensions are incorrect")
if png_dimensions(depth_png_path) != expected_depth:
    raise SystemExit("Saved depth preview dimensions are incorrect")

with depth_metadata_path.open(encoding="utf-8") as metadata_file:
    depth_metadata = json.load(metadata_file)
if (depth_metadata.get("width"), depth_metadata.get("height")) != expected_depth:
    raise SystemExit("Depth metadata dimensions are incorrect")
if depth_metadata.get("units") != "metres" or "float32" not in depth_metadata.get("encoding", ""):
    raise SystemExit("Depth metadata units or encoding are incorrect")
if not 0.0 < depth_metadata.get("min_range_m", 0.0) < depth_metadata.get("max_range_m", 0.0):
    raise SystemExit("Depth metadata range is invalid")
if depth_metadata.get("run_fingerprint_sha256") != fingerprint:
    raise SystemExit("Depth metadata and run manifest fingerprints disagree")
if depth_metadata.get("controller_config_sha256") != controller_config_sha256:
    raise SystemExit("Depth metadata and run manifest controller config SHA-256 disagree")
expected_depth_bytes = expected_depth[0] * expected_depth[1] * 4
if depth_raw_path.stat().st_size != expected_depth_bytes:
    raise SystemExit(
        f"Raw depth size is {depth_raw_path.stat().st_size}, expected {expected_depth_bytes} bytes"
    )

required_telemetry_fields = {
    "control_step",
    "sim_time_s",
    "host_monotonic_s",
    "run_seed",
    "apparatus_version_major",
    "apparatus_version_minor",
    "apparatus_version_patch",
    "course_zone_id",
    "x_m",
    "y_m",
    "z_m",
    "roll_rad",
    "pitch_rad",
    "yaw_rad",
    "gyro_x_radps",
    "gyro_y_radps",
    "gyro_z_radps",
    "command_throttle",
    "armed",
    "motor_front_left_rad_s",
    "motor_front_right_rad_s",
    "motor_rear_left_rad_s",
    "motor_rear_right_rad_s",
    "collision",
    "recovery_count",
    "input_source_code",
    "outage_active",
    "outage_condition_code",
    "outage_event_ordinal",
    "outage_seed",
    "outage_requested_duration_ms",
    "outage_effective_duration_ms",
    "outage_elapsed_ms",
    "outage_anchor_age_ms",
}
with telemetry_path.open(newline="", encoding="utf-8") as telemetry_file:
    reader = csv.DictReader(telemetry_file)
    missing = required_telemetry_fields.difference(reader.fieldnames or ())
    if missing:
        raise SystemExit(f"Telemetry schema is missing: {', '.join(sorted(missing))}")
    rows = list(reader)
if len(rows) < 10:
    raise SystemExit(f"Telemetry contains too few data rows: {len(rows)}")
valid_zone_ids = {0}
valid_zone_ids.update(
    int(zone["code"]) for zone in run_manifest["course_zones"]["zones"]
)
run_seed = run_manifest.get("random_seed")
if type(run_seed) is not int:
    raise SystemExit("Run manifest random seed is not an integer")
try:
    apparatus_version = tuple(int(part) for part in apparatus["version"].split("."))
except (KeyError, TypeError, ValueError) as error:
    raise SystemExit("Run manifest apparatus version is invalid") from error
if len(apparatus_version) != 3:
    raise SystemExit("Run manifest apparatus version is invalid")
expected_provenance = {
    "run_seed": str(run_seed),
    "apparatus_version_major": str(apparatus_version[0]),
    "apparatus_version_minor": str(apparatus_version[1]),
    "apparatus_version_patch": str(apparatus_version[2]),
}
times = []
for row_number, row in enumerate(rows, start=2):
    for field, expected in expected_provenance.items():
        if row.get(field) != expected:
            raise SystemExit(
                f"Telemetry row {row_number} has {field}={row.get(field)!r}; expected {expected!r}"
            )
    for field, encoded in row.items():
        try:
            value = float(encoded)
        except (TypeError, ValueError) as error:
            raise SystemExit(f"Telemetry row {row_number} has invalid {field}") from error
        if not math.isfinite(value):
            raise SystemExit(f"Telemetry row {row_number} has non-finite {field}")
    if int(float(row["course_zone_id"])) not in valid_zone_ids:
        raise SystemExit(f"Telemetry row {row_number} has an unknown course zone")
    times.append(float(row["sim_time_s"]))
for first, second in zip(times, times[1:]):
    gap = second - first
    if gap <= 0.0 or gap > 0.041:
        raise SystemExit(f"Telemetry timestamp gap is {gap * 1000:.1f} ms")

with outage_schedule_path.open(encoding="utf-8") as schedule_file:
    outage_schedule = json.load(schedule_file)
if outage_schedule.get("mode") != "off" or outage_schedule.get("events") != []:
    raise SystemExit("Normal smoke run has a non-empty outage schedule")
if outage_schedule.get("schedule_sha256") != marker.get("outage_schedule_sha256"):
    raise SystemExit("Smoke marker and outage schedule digests disagree")
events = [
    json.loads(line)
    for line in outage_events_path.read_text(encoding="utf-8").splitlines()
]
if [event.get("type") for event in events] != ["run_start", "run_end"]:
    raise SystemExit("Normal smoke run has unexpected outage events")
if events[-1].get("outcome") != "completed":
    raise SystemExit("Normal smoke run did not record completed outcome")
if any(events[-1].get(name) != 0 for name in (
    "scheduled_event_count",
    "started_event_count",
    "ended_event_count",
    "aborted_event_count",
    "pending_event_count",
)):
    raise SystemExit("Normal smoke run has unexpected outage event counts")
with display_frames_path.open(newline="", encoding="utf-8") as frame_file:
    display_rows = list(csv.DictReader(frame_file))
if display_rows:
    raise SystemExit("Normal smoke run unexpectedly logged display frames")

if marker["drone_z_m"] <= 0.25:
    raise SystemExit(
        f'Drone failed takeoff check: z={marker["drone_z_m"]:.3f} m'
    )
for axis in ("roll", "pitch", "yaw"):
    rate = marker[f"body_{axis}_rate_rad_s"]
    if abs(rate) >= 0.1:
        raise SystemExit(
            f"{axis.title()} rate damping failed: rate={rate:.3f} rad/s"
        )
idle_speed = float(flight_profile["motor_idle_speed"])
if marker["max_motor_speed_rad_s"] <= idle_speed + 1.0:
    raise SystemExit(
        "Direct throttle output did not rise above idle: "
        f'speed={marker["max_motor_speed_rad_s"]:.3f} rad/s'
    )
PY
then
  cat "$output_file" "$error_file"
  exit 1
fi

print "Webots smoke test passed ($row_count telemetry rows)."
