#!/usr/bin/env python3
"""Run a hidden, end-to-end acceptance test of Phase 2 video outages."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBOTS_APP = Path("/Applications/Webots.app")
WEBOTS = WEBOTS_APP / "Contents" / "MacOS" / "webots"
WORLD = PROJECT_ROOT / "worlds" / "dream_mode_research.wbt"
CONTROL_STEP_MS = 8
DISPLAY_PERIOD_MS = 16
RGB_SIZE = (480, 270)
DEPTH_SIZE = (320, 180)
CONDITIONS = ("black", "frozen")
DURATIONS_MS = (100, 250, 500, 750, 1000)

CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from outage_baselines import canonical_sha256
finally:
    sys.path.remove(str(CONTROLLER_DIR))


class BaselineValidationError(RuntimeError):
    """Raised when a hidden Webots run violates the Phase 2 contract."""


class WebotsStartupError(RuntimeError):
    """A retryable failure before the controller starts its run."""


def effective_duration(requested_ms: int) -> int:
    return math.ceil(requested_ms / DISPLAY_PERIOD_MS) * DISPLAY_PERIOD_MS


def scripted_events() -> tuple[list[dict], int]:
    events = []
    start_ms = 256
    ordinal = 0
    for duration_ms in DURATIONS_MS:
        for condition in CONDITIONS:
            ordinal += 1
            events.append(
                {
                    "id": f"acceptance_{ordinal:03d}",
                    "condition": condition,
                    "duration_ms": duration_ms,
                    "trigger": {"type": "time", "start_ms": start_ms},
                }
            )
            start_ms += effective_duration(duration_ms) + 128
    smoke_steps = math.ceil((start_ms + 256) / CONTROL_STEP_MS)
    return events, smoke_steps


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def webots_instance_pids(port: int) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", rf"^{re.escape(str(WEBOTS))} .*--port={port}( |$)"],
        check=False,
        capture_output=True,
        text=True,
    )
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def terminate_test_instance(port: int) -> None:
    for pid in webots_instance_pids(port):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and webots_instance_pids(port):
        time.sleep(0.05)
    for pid in webots_instance_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def launch_once(run_dir: Path, script: list[dict], smoke_steps: int) -> None:
    output_path = run_dir / "webots-output.txt"
    error_path = run_dir / "webots-error.txt"
    launcher_path = run_dir / "launcher-output.txt"
    marker_path = run_dir / "smoke_test_ok.json"
    manifest_path = run_dir / "run_manifest.json"
    port = available_port()
    render_viewpoint = os.environ.get("DREAM_MODE_RENDER_VIEWPOINT_TEST") == "1"
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("DREAM_MODE_"):
            environment.pop(name)
    environment.update(
        {
            "DREAM_MODE_OUTAGE_MODE": "scripted",
            "DREAM_MODE_OUTAGE_SCRIPT": json.dumps(
                script, separators=(",", ":"), sort_keys=True
            ),
            "DREAM_MODE_SMOKE_STEPS": str(smoke_steps),
            "DREAM_MODE_COMMAND_SEQUENCE": json.dumps(
                [
                    [0.0, 0.0, 0.0, 0.18, 0.323],
                    [smoke_steps * CONTROL_STEP_MS / 1000.0, 0.0, 0.0, 0.18, 0.323],
                ],
                separators=(",", ":"),
            ),
            "DREAM_MODE_INITIAL_ALTITUDE": "5.0",
            "DREAM_MODE_LOG_PERIOD_SECONDS": "0.008",
            "DREAM_MODE_BYPASS_ARMING": "1",
            "DREAM_MODE_DISABLE_JOYSTICK": "1",
            "DREAM_MODE_DISABLE_RECOVERY": "1",
            "DREAM_MODE_VALIDATE_DISPLAY_READBACK": "1",
            "DREAM_MODE_LOG_DIR": str(run_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM": "1",
        }
    )
    if render_viewpoint:
        environment["DREAM_MODE_CAPTURE_VIEWPOINT"] = "1"
    command = [
        "/usr/bin/open",
        "-F",
        "-g",
        "-j",
        "-n",
        "-a",
        str(WEBOTS_APP),
        "-o",
        str(output_path),
        "--stderr",
        str(error_path),
    ]
    for name, value in sorted(environment.items()):
        if name.startswith("DREAM_MODE_") or name in {
            "PYTHONDONTWRITEBYTECODE",
            "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM",
        }:
            command.extend(["--env", f"{name}={value}"])
    webots_arguments = ["--args", "--batch"]
    if not render_viewpoint:
        webots_arguments.append("--no-rendering")
    webots_arguments.extend(
        [
            "--mode=fast",
            f"--port={port}",
            "--stdout",
            "--stderr",
            str(WORLD),
        ]
    )
    command.extend(webots_arguments)
    with launcher_path.open("x", encoding="utf-8") as launcher_output:
        launch = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=launcher_output,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=8,
        )
    if launch.returncode != 0:
        raise WebotsStartupError(
            f"macOS launcher exited {launch.returncode}; see {launcher_path}"
        )

    seen_instance = False
    started = time.monotonic()
    try:
        while not marker_path.is_file():
            active = webots_instance_pids(port)
            seen_instance = seen_instance or bool(active)
            elapsed = time.monotonic() - started
            if seen_instance and not active:
                raise WebotsStartupError(
                    f"Webots exited before completion; see {output_path}"
                )
            if not manifest_path.is_file() and elapsed >= 15.0:
                raise WebotsStartupError(
                    f"controller did not initialize; see {error_path}"
                )
            if elapsed >= 50.0:
                raise BaselineValidationError(
                    f"simulation timed out; see {output_path}"
                )
            time.sleep(0.05)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and webots_instance_pids(port):
            time.sleep(0.05)
    finally:
        terminate_test_instance(port)


def run_with_retries(root: Path, label: str, script: list[dict], steps: int) -> Path:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        run_dir = root / f"{label}_attempt_{attempt}"
        run_dir.mkdir()
        try:
            launch_once(run_dir, script, steps)
            return run_dir
        except WebotsStartupError as error:
            last_error = error
            if attempt < 3:
                time.sleep(attempt)
    assert last_error is not None
    raise last_error


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def load_json_lines(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def png_rgb(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise BaselineValidationError(f"not a PNG: {path}")
    offset = 8
    header = None
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    if header is None:
        raise BaselineValidationError(f"PNG has no IHDR: {path}")
    width, height, bit_depth, color_type, compression, filtering, interlace = header
    channels = {2: 3, 6: 4}.get(color_type)
    if (
        bit_depth != 8
        or channels is None
        or compression != 0
        or filtering != 0
        or interlace != 0
    ):
        raise BaselineValidationError(f"unsupported PNG encoding in {path}")
    decoded = zlib.decompress(bytes(compressed))
    stride = width * channels
    rows = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        current = bytearray(decoded[cursor : cursor + stride])
        cursor += stride
        for index in range(stride):
            left = current[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                current[index] = (current[index] + left) & 0xFF
            elif filter_type == 2:
                current[index] = (current[index] + above) & 0xFF
            elif filter_type == 3:
                current[index] = (current[index] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                current[index] = (
                    current[index] + _paeth(left, above, upper_left)
                ) & 0xFF
            elif filter_type != 0:
                raise BaselineValidationError(
                    f"unsupported PNG row filter {filter_type} in {path}"
                )
        rows.append(bytes(current))
        previous = current
    rgb = bytearray(width * height * 3)
    destination = 0
    for row in rows:
        for source in range(0, len(row), channels):
            rgb[destination : destination + 3] = row[source : source + 3]
            destination += 3
    return width, height, bytes(rgb)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise BaselineValidationError(message)


def validate_artifacts(run_dir: Path, expected_script: list[dict]) -> dict:
    required = (
        "run_manifest.json",
        "outage_schedule.json",
        "outage_events.jsonl",
        "display_frames.csv",
        "telemetry.csv",
        "smoke_test_ok.json",
    )
    for name in required:
        require((run_dir / name).is_file(), f"missing {name} in {run_dir}")

    manifest = load_json(run_dir / "run_manifest.json")
    schedule = load_json(run_dir / "outage_schedule.json")
    marker = load_json(run_dir / "smoke_test_ok.json")
    events = load_json_lines(run_dir / "outage_events.jsonl")
    unhashed_schedule = {
        key: value for key, value in schedule.items() if key != "schedule_sha256"
    }
    require(
        schedule.get("schedule_sha256") == canonical_sha256(unhashed_schedule),
        "materialized outage schedule digest is invalid",
    )
    require(schedule.get("mode") == "scripted", "run did not use scripted mode")
    require(len(schedule.get("events", [])) == len(expected_script), "wrong event count")
    require(
        marker.get("outage_schedule_sha256") == schedule["schedule_sha256"],
        "marker and schedule digests disagree",
    )
    outage_manifest = manifest.get("outage_baseline", {})
    require(outage_manifest.get("mode") == "scripted", "manifest mode is wrong")
    require(
        outage_manifest.get("schedule_sha256") == schedule["schedule_sha256"],
        "manifest and schedule digests disagree",
    )
    safety = outage_manifest.get("research_overlay_safety", {})
    require(
        safety.get("research_camera_visible") is False
        and safety.get("depth_visible") is False,
        "hidden-truth rendering overlays were not proven hidden",
    )
    require(
        manifest.get("runtime", {}).get("continuous_research_sensors") is True,
        "hidden RGB-D did not remain active during outage testing",
    )
    require(
        outage_manifest.get("physical_display_readback") is True,
        "acceptance run did not enable physical Display readback",
    )

    event_types = [event.get("type") for event in events]
    require(event_types.count("run_start") == 1, "run_start count is not one")
    require(event_types.count("start") == len(expected_script), "start count is wrong")
    require(event_types.count("end") == len(expected_script), "end count is wrong")
    require("abort" not in event_types, "an outage was aborted")
    require("scheduler_error" not in event_types, "the scheduler reported an error")
    require(event_types[-1] == "run_end", "run_end is missing or out of order")
    run_end = events[-1]
    require(run_end.get("outcome") == "completed", "run outcome is not completed")
    for name, expected in (
        ("scheduled_event_count", len(expected_script)),
        ("started_event_count", len(expected_script)),
        ("ended_event_count", len(expected_script)),
        ("aborted_event_count", 0),
        ("pending_event_count", 0),
    ):
        require(run_end.get(name) == expected, f"run_end {name} is wrong")

    starts = {event["event"]["id"]: event for event in events if event["type"] == "start"}
    ends = {event["event"]["id"]: event for event in events if event["type"] == "end"}
    with (run_dir / "display_frames.csv").open(newline="", encoding="utf-8") as source:
        frame_rows = list(csv.DictReader(source))
    require(frame_rows, "display frame log is empty")
    frames_by_step = {int(row["control_step"]): row for row in frame_rows}
    frames_by_event: dict[str, list[dict[str, str]]] = {}
    for row in frame_rows:
        if row["event_id"]:
            frames_by_event.setdefault(row["event_id"], []).append(row)

    black_hash = hashlib.sha256(
        bytes((0, 0, 0, 255)) * (RGB_SIZE[0] * RGB_SIZE[1])
    ).hexdigest()
    processing_samples = []
    transition_signature = []
    for scheduled, requested in zip(schedule["events"], expected_script):
        event_id = scheduled["id"]
        require(event_id == requested["id"], f"event order changed for {event_id}")
        start_step = int(scheduled["trigger"]["planned_start_step"])
        end_step = start_step + int(scheduled["duration_control_steps"])
        start = starts[event_id]
        end = ends[event_id]
        require(start["command_step"] == start_step, f"{event_id} started late")
        require(
            start["visible_from_step"] == start_step + 1,
            f"{event_id} visibility latency is wrong",
        )
        require(end["command_step"] == end_step, f"{event_id} ended late")
        require(
            end["live_visible_from_step"] == end_step + 1,
            f"{event_id} live-return latency is wrong",
        )
        require(
            end["actual_duration_ms"] == scheduled["effective_duration_ms"],
            f"{event_id} realized duration is wrong",
        )
        processing_samples.append(float(start["controller_processing_ms"]))
        transition_signature.extend(
            (("start", event_id, start_step), ("end", event_id, end_step))
        )

        expected_frame_steps = list(range(start_step + 2, end_step + 1, 2))
        event_frames = frames_by_event.get(event_id, [])
        require(
            [int(row["control_step"]) for row in event_frames] == expected_frame_steps,
            f"{event_id} pilot-frame interval is not exact",
        )
        require(
            all(row["display_mode"] == scheduled["condition"] for row in event_frames),
            f"{event_id} frame mode is inconsistent",
        )
        require(
            frames_by_step[start_step]["display_mode"] == "normal",
            f"{event_id} command step was logged as already interrupted",
        )
        require(
            frames_by_step[end_step + 2]["display_mode"] == "normal",
            f"{event_id} did not return to live video",
        )

        rendered_hashes = {row["rendered_sha256"] for row in event_frames}
        hidden_hashes = {row["hidden_ground_truth_sha256"] for row in event_frames}
        require(len(hidden_hashes) > 1, f"hidden scene did not change during {event_id}")
        if scheduled["condition"] == "black":
            require(rendered_hashes == {black_hash}, f"{event_id} was not true black")
            require(
                all(row["source_type"] == "black" for row in event_frames),
                f"{event_id} black source labels are wrong",
            )
        else:
            anchor_hash = start["anchor_sha256"]
            require(rendered_hashes == {anchor_hash}, f"{event_id} freeze frame changed")
            require(
                all(row["source_type"] == "anchor" for row in event_frames),
                f"{event_id} freeze source labels are wrong",
            )
            require(
                len({row["source_frame_id"] for row in event_frames}) == 1,
                f"{event_id} freeze source frame changed",
            )

        artifact_dir = run_dir / start["artifact_directory"]
        expected_files = (
            "anchor_rgb.png",
            "anchor_depth.png",
            "anchor_depth.f32",
            "anchor_depth.json",
            "pilot_anchor.png",
            "pilot_source_anchor.png",
            "hidden_mid_rgb.png",
            "pilot_mid.png",
            "pilot_last.png",
            "pilot_return.png",
            "return_rgb.png",
        )
        for name in expected_files:
            require(
                (artifact_dir / name).is_file()
                and (artifact_dir / name).stat().st_size > 0,
                f"{event_id} is missing {name}",
            )
        depth_metadata = load_json(artifact_dir / "anchor_depth.json")
        require(
            (depth_metadata.get("width"), depth_metadata.get("height")) == DEPTH_SIZE,
            f"{event_id} depth metadata dimensions are wrong",
        )
        require(
            (artifact_dir / "anchor_depth.f32").stat().st_size
            == DEPTH_SIZE[0] * DEPTH_SIZE[1] * 4,
            f"{event_id} depth raw size is wrong",
        )
        anchor_width, anchor_height, anchor_rgb = png_rgb(
            artifact_dir / "pilot_anchor.png"
        )
        _, _, pilot_source_anchor_rgb = png_rgb(
            artifact_dir / "pilot_source_anchor.png"
        )
        _, _, recorded_anchor_rgb = png_rgb(artifact_dir / "anchor_rgb.png")
        mid_width, mid_height, mid_rgb = png_rgb(artifact_dir / "pilot_mid.png")
        last_width, last_height, last_rgb = png_rgb(artifact_dir / "pilot_last.png")
        return_width, return_height, return_pilot_rgb = png_rgb(
            artifact_dir / "pilot_return.png"
        )
        require(
            (anchor_width, anchor_height) == RGB_SIZE,
            f"{event_id} anchor dimensions are wrong",
        )
        require(
            anchor_rgb == pilot_source_anchor_rgb,
            f"{event_id} physical Display anchor differs from the pilot source",
        )
        require(
            pilot_source_anchor_rgb != recorded_anchor_rgb,
            f"{event_id} pilot source is not visually degraded",
        )
        require((mid_width, mid_height) == RGB_SIZE, f"{event_id} mid dimensions are wrong")
        require((last_width, last_height) == RGB_SIZE, f"{event_id} last dimensions are wrong")
        require(
            (return_width, return_height) == RGB_SIZE,
            f"{event_id} return dimensions are wrong",
        )
        require(mid_rgb == last_rgb, f"{event_id} pilot image changed before return")
        require(
            return_pilot_rgb != last_rgb,
            f"{event_id} pilot display did not visibly return to live video",
        )
        if scheduled["condition"] == "black":
            require(not any(mid_rgb), f"{event_id} physical Display was not black")
        else:
            require(mid_rgb == anchor_rgb, f"{event_id} physical freeze anchor changed")

    require(
        max(processing_samples) <= 16.0,
        "outage onset processing exceeded one 16 ms display period: "
        f"{max(processing_samples):.3f} ms",
    )

    with (run_dir / "telemetry.csv").open(newline="", encoding="utf-8") as source:
        telemetry = {
            int(row["control_step"]): row for row in csv.DictReader(source)
        }
    condition_codes = {"black": "1", "frozen": "2"}
    for event in schedule["events"]:
        start_step = int(event["trigger"]["planned_start_step"])
        end_step = start_step + int(event["duration_control_steps"])
        require(
            telemetry[start_step]["outage_active"] == "1"
            and telemetry[start_step]["outage_condition_code"]
            == condition_codes[event["condition"]],
            f"telemetry missed the start of {event['id']}",
        )
        require(
            telemetry[end_step]["outage_active"] == "0"
            and telemetry[end_step]["outage_condition_code"] == "0",
            f"telemetry missed the end of {event['id']}",
        )

    return {
        "schedule": schedule,
        "transition_signature": transition_signature,
        "max_onset_processing_ms": max(processing_samples),
        "frame_signature": [
            (int(row["control_step"]), row["display_mode"], row["event_id"])
            for row in frame_rows
        ],
    }


def main() -> int:
    if not WEBOTS.is_file():
        print(f"Webots was not found at {WEBOTS}", file=sys.stderr)
        return 1
    script, smoke_steps = scripted_events()
    keep = os.environ.pop("DREAM_MODE_KEEP_OUTAGE_TEST", "0") == "1"
    root = Path(tempfile.mkdtemp(prefix="dream-mode-outage-test."))
    success = False
    try:
        first_dir = run_with_retries(root, "first", script, smoke_steps)
        second_dir = run_with_retries(root, "replay", script, smoke_steps)
        first = validate_artifacts(first_dir, script)
        second = validate_artifacts(second_dir, script)
        require(first["schedule"] == second["schedule"], "replay schedule changed")
        require(
            first["transition_signature"] == second["transition_signature"],
            "replay transition timing changed",
        )
        require(
            first["frame_signature"] == second["frame_signature"],
            "replay pilot-mode timing changed",
        )
        success = True
        maximum = max(
            first["max_onset_processing_ms"],
            second["max_onset_processing_ms"],
        )
        print(
            "Phase 2 outage baseline passed: "
            f"2 replays, {len(script)} events each, max onset work {maximum:.3f} ms."
        )
        return 0
    except (BaselineValidationError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Phase 2 outage baseline failed: {error}", file=sys.stderr)
        print(f"Artifacts preserved at {root}", file=sys.stderr)
        return 1
    finally:
        if success and not keep:
            shutil.rmtree(root)
        elif success:
            print(f"Preserved outage-test artifacts: {root}")


if __name__ == "__main__":
    raise SystemExit(main())
