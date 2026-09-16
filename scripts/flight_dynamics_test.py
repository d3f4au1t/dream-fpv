#!/usr/bin/env python3
"""Run hidden, end-to-end Webots acceptance tests for the FPV flight model."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
import json
import math
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBOTS = Path("/Applications/Webots.app/Contents/MacOS/webots")
WORLD = PROJECT_ROOT / "worlds" / "dream_mode_research.wbt"
TIME_STEP_SECONDS = 0.008
MOTOR_NAMES = ("front_left", "front_right", "rear_left", "rear_right")
AXES = ("roll", "pitch", "yaw")
AXIS_COMMAND_INDEX = {"roll": 1, "pitch": 2, "yaw": 3}
GYRO_FIELD = {
    "roll": "gyro_x_radps",
    "pitch": "gyro_y_radps",
    "yaw": "gyro_z_radps",
}
# Independent golden definition of the accepted pilot profile. A positive
# scripted command maps to these Webots body-rate signs.
EXPECTED_BODY_SIGN = {"roll": -1, "pitch": -1, "yaw": 1}
EXPECTED_RATES = {
    "roll": (1.25, 0.68, 0.22),
    "pitch": (1.25, 0.68, 0.22),
    "yaw": (1.25, 0.55, 0.28),
}

with (PROJECT_ROOT / "config" / "controller.json").open(encoding="utf-8") as file:
    CONFIG = json.load(file)
FLIGHT_PROFILE = CONFIG["flight_profile"]
IDLE_SPEED = float(FLIGHT_PROFILE["motor_idle_speed"])
MAX_MOTOR_SPEED = float(FLIGHT_PROFILE["motor_max_speed"])
HOVER_THROTTLE = float(FLIGHT_PROFILE["keyboard_hover_throttle"])


@dataclass(frozen=True)
class Scenario:
    name: str
    sequence: list[list[float]]
    duration_s: float
    kind: str
    axis: str | None = None
    magnitude: float | None = None
    direction: int | None = None
    throttle: float | None = None
    bypass_arming: bool = True
    initial_altitude: float = 20.0
    recovery_enabled: bool = False
    emergency_stop_time: float | None = None
    explicit_arm_time: float | None = None
    input_loss_time: float | None = None
    inverted_crash_step: int | None = None


@dataclass
class Result:
    scenario: Scenario
    rows: list[dict[str, float]]
    log_dir: Path
    run_manifest: dict


def command_point(time_s: float, axes: dict[str, float], throttle: float) -> list[float]:
    return [
        time_s,
        axes.get("roll", 0.0),
        axes.get("pitch", 0.0),
        axes.get("yaw", 0.0),
        throttle,
    ]


def expected_rate(axis: str, stick: float) -> float:
    rc_rate, super_rate, expo = EXPECTED_RATES[axis]
    magnitude = abs(stick)
    curved_stick = stick * (expo * magnitude**3 + 1.0 - expo)
    if rc_rate > 2.0:
        rc_rate += 14.54 * (rc_rate - 2.0)
    return 200.0 * rc_rate * curved_stick / max(
        0.01,
        1.0 - magnitude * super_rate,
    )


def axis_scenario(
    axis: str,
    magnitude: float,
    direction: int,
    throttle: float,
    suffix: str = "",
) -> Scenario:
    label = f"{axis}_{magnitude:.2f}_{'pos' if direction > 0 else 'neg'}{suffix}"
    return Scenario(
        name=label,
        sequence=[
            command_point(0.0, {}, throttle),
            command_point(0.40, {axis: magnitude * direction}, throttle),
            command_point(0.80, {}, throttle),
        ],
        duration_s=1.20,
        kind="axis",
        axis=axis,
        magnitude=magnitude,
        direction=direction,
        throttle=throttle,
    )


def mixed_scenario(name: str, commands: dict[str, float]) -> Scenario:
    return Scenario(
        name=name,
        sequence=[
            command_point(0.0, {}, HOVER_THROTTLE),
            command_point(0.40, commands, HOVER_THROTTLE),
            command_point(0.80, {}, HOVER_THROTTLE),
        ],
        duration_s=1.24,
        kind="mixed",
        throttle=HOVER_THROTTLE,
    )


def build_stress_sequence() -> list[list[float]]:
    generator = random.Random(1907)
    sequence = [command_point(0.0, {}, HOVER_THROTTLE)]
    time_s = 0.20
    while time_s < 6.20:
        axes = {
            "roll": generator.uniform(-0.85, 0.85),
            "pitch": generator.uniform(-0.85, 0.85),
            "yaw": generator.uniform(-0.75, 0.75),
        }
        sequence.append(
            command_point(time_s, axes, generator.uniform(0.16, 0.44))
        )
        time_s += 0.064
    sequence.append(command_point(6.20, {}, HOVER_THROTTLE))
    return sequence


def build_scenarios() -> list[Scenario]:
    scenarios: list[Scenario] = []
    for axis in AXES:
        for magnitude in (0.25, 0.50, 1.00):
            for direction in (-1, 1):
                scenarios.append(
                    axis_scenario(axis, magnitude, direction, HOVER_THROTTLE)
                )

    for axis in AXES:
        scenarios.append(axis_scenario(axis, 0.25, 1, 0.12, "_low_throttle"))
        scenarios.append(axis_scenario(axis, 0.25, 1, 0.75, "_high_throttle"))

    scenarios.extend(
        [
            mixed_scenario(
                "mixed_axes_moderate_a",
                {"roll": 0.35, "pitch": -0.30, "yaw": 0.25},
            ),
            mixed_scenario(
                "mixed_axes_moderate_b",
                {"roll": -0.35, "pitch": 0.30, "yaw": -0.25},
            ),
        ]
    )
    for first, second in (("roll", "pitch"), ("roll", "yaw"), ("pitch", "yaw")):
        for signs in ((1, 1), (1, -1)):
            label = "_".join("pos" if sign > 0 else "neg" for sign in signs)
            scenarios.append(
                mixed_scenario(
                    f"mixed_axes_full_pair_{first}_{second}_{label}",
                    {first: float(signs[0]), second: float(signs[1])},
                )
            )
    for signs in product((-1, 1), repeat=3):
        label = "_".join("pos" if sign > 0 else "neg" for sign in signs)
        scenarios.append(
            mixed_scenario(
                f"mixed_axes_full_triple_{label}",
                dict(zip(AXES, map(float, signs))),
            )
        )

    for name, throttle, duration in (
        ("zero_throttle", 0.0, 1.0),
        ("low_throttle", 0.12, 1.0),
        ("hover_throttle", HOVER_THROTTLE, 1.2),
        ("high_throttle", 0.50, 1.0),
        ("full_throttle", 1.0, 0.6),
    ):
        scenarios.append(
            Scenario(
                name=name,
                sequence=[command_point(0.0, {}, throttle)],
                duration_s=duration,
                kind="throttle",
                throttle=throttle,
                initial_altitude=10.0,
            )
        )

    scenarios.extend(
        [
            Scenario(
                name="arming_rejects_high_throttle",
                sequence=[command_point(0.0, {}, 0.35)],
                duration_s=0.50,
                kind="arm_reject",
                throttle=0.35,
                bypass_arming=False,
                initial_altitude=5.0,
            ),
            Scenario(
                name="arming_low_then_fly",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.20, {}, 0.36),
                ],
                duration_s=0.80,
                kind="arm_accept",
                throttle=0.36,
                bypass_arming=False,
                initial_altitude=5.0,
            ),
            Scenario(
                name="ground_zero_throttle_full_sticks",
                sequence=[
                    command_point(
                        0.0,
                        {"roll": 1.0, "pitch": 1.0, "yaw": 1.0},
                        0.0,
                    )
                ],
                duration_s=0.55,
                kind="ground_safety",
                throttle=0.0,
                initial_altitude=0.03,
            ),
        ]
    )

    for axis in AXES:
        for direction in (-1, 1):
            scenarios.append(
                Scenario(
                    name=f"reversal_{axis}_{'pos_neg' if direction > 0 else 'neg_pos'}",
                    sequence=[
                        command_point(0.0, {}, HOVER_THROTTLE),
                        command_point(0.35, {axis: float(direction)}, HOVER_THROTTLE),
                        command_point(0.60, {axis: float(-direction)}, HOVER_THROTTLE),
                        command_point(0.90, {}, HOVER_THROTTLE),
                    ],
                    duration_s=1.25,
                    kind="reversal",
                    axis=axis,
                    magnitude=1.0,
                    direction=direction,
                    throttle=HOVER_THROTTLE,
                )
            )

    for axis in ("roll", "pitch"):
        for direction in (-1, 1):
            scenarios.append(
                Scenario(
                    name=f"acro_retention_{axis}_{'pos' if direction > 0 else 'neg'}",
                    sequence=[
                        command_point(0.0, {}, HOVER_THROTTLE),
                        command_point(0.40, {axis: 0.50 * direction}, HOVER_THROTTLE),
                        command_point(0.56, {}, HOVER_THROTTLE),
                    ],
                    duration_s=1.45,
                    kind="acro",
                    axis=axis,
                    magnitude=0.50,
                    direction=direction,
                    throttle=HOVER_THROTTLE,
                )
            )

    scenarios.extend(
        [
            Scenario(
                name="airborne_zero_throttle_airmode",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(
                        0.30,
                        {"roll": 1.0, "pitch": 1.0, "yaw": 1.0},
                        0.0,
                    ),
                    command_point(0.70, {}, 0.0),
                ],
                duration_s=1.0,
                kind="airmode",
                throttle=0.0,
                initial_altitude=20.0,
            ),
            Scenario(
                name="land_then_zero_throttle_full_sticks",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(
                        2.20,
                        {"roll": 1.0, "pitch": 1.0, "yaw": 1.0},
                        0.0,
                    ),
                ],
                duration_s=2.75,
                kind="landed_safety",
                throttle=0.0,
                initial_altitude=2.0,
            ),
            Scenario(
                name="boundary_recovery_and_rearm",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.12, {}, 1.0),
                    # A held attitude command must not survive the teleport
                    # into a sideways launch when throttle reaches low.
                    command_point(1.30, {"pitch": 0.50}, 0.0),
                    command_point(1.50, {}, 0.0),
                    command_point(1.72, {}, 0.36),
                ],
                duration_s=2.40,
                kind="recovery",
                bypass_arming=False,
                initial_altitude=0.03,
                recovery_enabled=True,
            ),
            Scenario(
                name="drop_collision_and_takeoff",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(1.10, {}, 0.36),
                ],
                duration_s=1.85,
                kind="crash",
                initial_altitude=2.0,
            ),
            Scenario(
                name="inverted_crash_auto_recovery",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(1.35, {}, 0.36),
                ],
                duration_s=2.20,
                kind="inverted_recovery",
                initial_altitude=0.03,
                recovery_enabled=True,
                inverted_crash_step=30,
            ),
            Scenario(
                name="emergency_stop_and_rearm",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.15, {}, 0.50),
                    command_point(0.75, {}, 0.0),
                    command_point(0.95, {}, 0.36),
                ],
                duration_s=1.55,
                kind="emergency",
                bypass_arming=False,
                initial_altitude=5.0,
                emergency_stop_time=0.50,
                explicit_arm_time=0.82,
            ),
            Scenario(
                name="low_throttle_emergency_latches",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(1.00, {}, 0.36),
                ],
                duration_s=1.45,
                kind="emergency_low",
                bypass_arming=False,
                initial_altitude=2.0,
                emergency_stop_time=0.20,
                explicit_arm_time=0.85,
            ),
            Scenario(
                name="pre_stop_arm_press_is_not_queued",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.15, {}, 0.50),
                    command_point(0.75, {}, 0.0),
                ],
                duration_s=1.15,
                kind="emergency_reject",
                bypass_arming=False,
                initial_altitude=5.0,
                emergency_stop_time=0.50,
                explicit_arm_time=0.30,
            ),
            Scenario(
                name="high_throttle_arm_press_is_not_queued",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.15, {}, 0.50),
                    command_point(0.75, {}, 0.0),
                ],
                duration_s=1.15,
                kind="emergency_reject",
                bypass_arming=False,
                initial_altitude=5.0,
                emergency_stop_time=0.50,
                explicit_arm_time=0.60,
            ),
            Scenario(
                name="input_loss_requires_fresh_low_source",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.15, {}, 0.50),
                    command_point(0.75, {}, 0.0),
                    command_point(0.95, {}, 0.36),
                ],
                duration_s=1.50,
                kind="input_loss",
                bypass_arming=False,
                initial_altitude=5.0,
                input_loss_time=0.50,
            ),
            Scenario(
                name="low_throttle_input_loss_requires_gesture",
                sequence=[
                    command_point(0.0, {}, 0.0),
                    command_point(0.72, {}, 0.20),
                    command_point(0.85, {}, 0.0),
                    command_point(1.00, {}, 0.36),
                ],
                duration_s=1.45,
                kind="input_loss_low",
                bypass_arming=False,
                initial_altitude=2.0,
                input_loss_time=0.20,
            ),
            Scenario(
                name="deterministic_command_stress",
                sequence=build_stress_sequence(),
                duration_s=6.75,
                kind="stress",
                initial_altitude=100.0,
            ),
        ]
    )

    replay_sequence = [
        command_point(0.0, {}, HOVER_THROTTLE),
        command_point(0.40, {"roll": 0.50}, HOVER_THROTTLE),
        command_point(0.80, {}, HOVER_THROTTLE),
    ]
    for suffix in ("a", "b"):
        scenarios.append(
            Scenario(
                name=f"determinism_replay_{suffix}",
                sequence=replay_sequence,
                duration_s=1.20,
                kind="axis",
                axis="roll",
                magnitude=0.50,
                direction=1,
                throttle=HOVER_THROTTLE,
            )
        )
    return scenarios


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def terminate_test_instance(process: subprocess.Popen) -> None:
    """Terminate one hidden Webots process group without touching the live UI."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=3)


def run_scenario(scenario: Scenario, index: int, run_root: Path) -> Result:
    log_dir = run_root / scenario.name
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / "webots-output.txt"
    steps = math.ceil(scenario.duration_s / TIME_STEP_SECONDS)
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("DREAM_MODE_"):
            environment.pop(name)
    environment.update({
        "DREAM_MODE_SMOKE_STEPS": str(steps),
        "DREAM_MODE_COMMAND_SEQUENCE": json.dumps(scenario.sequence),
        "DREAM_MODE_INITIAL_ALTITUDE": str(scenario.initial_altitude),
        "DREAM_MODE_LOG_PERIOD_SECONDS": str(TIME_STEP_SECONDS),
        "DREAM_MODE_DISABLE_JOYSTICK": "1",
        "DREAM_MODE_LOG_DIR": str(log_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        # Prevent Qt from transforming this command-line process into the
        # foreground macOS application. This is what keeps user focus intact.
        "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM": "1",
    })
    if not scenario.recovery_enabled:
        environment["DREAM_MODE_DISABLE_RECOVERY"] = "1"
    if scenario.bypass_arming:
        environment["DREAM_MODE_BYPASS_ARMING"] = "1"
    if scenario.emergency_stop_time is not None:
        environment["DREAM_MODE_EMERGENCY_STOP_TIME"] = str(
            scenario.emergency_stop_time
        )
    if scenario.explicit_arm_time is not None:
        environment["DREAM_MODE_EXPLICIT_ARM_TIME"] = str(
            scenario.explicit_arm_time
        )
    if scenario.input_loss_time is not None:
        environment["DREAM_MODE_TEST_INPUT_LOSS_TIME"] = str(
            scenario.input_loss_time
        )
    if scenario.inverted_crash_step is not None:
        environment["DREAM_MODE_INVERTED_CRASH_STEP"] = str(
            scenario.inverted_crash_step
        )
    environment["DREAM_MODE_SKIP_INITIAL_CAPTURE"] = "1"

    port = available_port()
    command = [
        str(WEBOTS),
        "--batch",
        "--no-rendering",
        "--mode=fast",
        f"--port={port}",
        "--stdout",
        "--stderr",
        str(WORLD),
    ]
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=35)
        except subprocess.TimeoutExpired as error:
            terminate_test_instance(process)
            raise RuntimeError(f"timed out after {error.timeout:.0f} seconds") from error
    if return_code != 0:
        raise RuntimeError(f"Webots exited {return_code}; see {output_path}")

    telemetry_path = log_dir / "telemetry.csv"
    marker_path = log_dir / "smoke_test_ok.json"
    run_manifest_path = log_dir / "run_manifest.json"
    if (
        not telemetry_path.is_file()
        or not marker_path.is_file()
        or not run_manifest_path.is_file()
    ):
        raise RuntimeError("simulation did not finish cleanly")
    with run_manifest_path.open(encoding="utf-8") as manifest_file:
        run_manifest = json.load(manifest_file)
    apparatus = run_manifest.get("apparatus", {})
    if apparatus.get("id") != "dream_fpv_webots" or apparatus.get("version") != "1.0.0":
        raise RuntimeError(f"unexpected apparatus identity: {apparatus}")
    with telemetry_path.open(newline="", encoding="utf-8") as telemetry_file:
        rows = [
            {key: float(value) for key, value in row.items()}
            for row in csv.DictReader(telemetry_file)
        ]
    return Result(
        scenario=scenario,
        rows=rows,
        log_dir=log_dir,
        run_manifest=run_manifest,
    )


def first_time(rows: list[dict[str, float]], predicate) -> float | None:
    for row in rows:
        if predicate(row):
            return row["sim_time_s"]
    return None


def window(rows: list[dict[str, float]], start: float, end: float) -> list[dict[str, float]]:
    return [row for row in rows if start <= row["sim_time_s"] < end]


def body_rate_deg_s(row: dict[str, float], axis: str) -> float:
    return math.degrees(row[GYRO_FIELD[axis]])


def motor_values(row: dict[str, float]) -> list[float]:
    return [row[f"motor_{name}_rad_s"] for name in MOTOR_NAMES]


def assert_common(result: Result, failures: list[str]) -> bool:
    name = result.scenario.name
    if not result.rows:
        failures.append(f"{name}: telemetry is empty")
        return False
    previous_time = None
    for row in result.rows:
        for field, value in row.items():
            if not math.isfinite(value):
                failures.append(f"{name}: non-finite {field}")
                return False
        if previous_time is not None:
            gap = row["sim_time_s"] - previous_time
            if gap <= 0.0 or gap > 0.0121:
                failures.append(f"{name}: telemetry gap was {gap * 1000:.1f} ms")
                return False
        previous_time = row["sim_time_s"]
        motors = motor_values(row)
        if min(motors) < -1e-6 or max(motors) > MAX_MOTOR_SPEED + 0.001:
            failures.append(f"{name}: motor command outside configured bounds")
            return False
        if row["armed"] < 0.5 and max(motors) > 0.001:
            failures.append(f"{name}: motor commanded while disarmed")
            return False
        if row["armed"] >= 0.5 and min(motors) < IDLE_SPEED - 0.001:
            failures.append(f"{name}: armed motor fell below idle")
            return False
    if result.rows[-1]["sim_time_s"] < result.scenario.duration_s - 0.016:
        failures.append(f"{name}: telemetry ended before the requested duration")
        return False
    return True


def analyze_axis(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    scenario = result.scenario
    assert scenario.axis and scenario.magnitude is not None and scenario.direction
    axis = scenario.axis
    expected_target = (
        EXPECTED_BODY_SIGN[axis]
        * scenario.direction
        * abs(expected_rate(axis, scenario.magnitude))
    )
    active = window(result.rows, 0.40, 0.80)
    if not active:
        failures.append(f"{scenario.name}: missing active command window")
        return
    logged_target = sum(
        row[f"target_{axis}_rate_deg_s"] for row in active[-10:]
    ) / len(active[-10:])
    if abs(logged_target - expected_target) > 0.1:
        failures.append(
            f"{scenario.name}: target was {logged_target:.1f}, expected {expected_target:.1f} deg/s"
        )
    direction = math.copysign(1.0, expected_target)
    target_magnitude = abs(expected_target)

    def directed_rate(row: dict[str, float]) -> float:
        return direction * body_rate_deg_s(row, axis)

    onset = first_time(active, lambda row: directed_rate(row) >= 0.10 * target_magnitude)
    ninety = first_time(active, lambda row: directed_rate(row) >= 0.90 * target_magnitude)
    if onset is None or ninety is None:
        failures.append(f"{scenario.name}: failed to reach commanded body rate")
        return
    onset_latency = onset - 0.40
    ninety_latency = ninety - 0.40
    if onset_latency > 0.0161:
        failures.append(f"{scenario.name}: response onset took {onset_latency * 1000:.0f} ms")
    latency_limit = {0.25: 0.056, 0.50: 0.064, 1.00: 0.100}[scenario.magnitude]
    if ninety_latency > latency_limit + 1e-6:
        failures.append(
            f"{scenario.name}: 90% response took {ninety_latency * 1000:.0f} ms "
            f"(limit {latency_limit * 1000:.0f} ms)"
        )

    peak = max(directed_rate(row) for row in active)
    overshoot = max(0.0, peak / target_magnitude - 1.0)
    if overshoot > 0.10:
        failures.append(f"{scenario.name}: {overshoot * 100:.1f}% rate overshoot")
    settled = window(result.rows, 0.68, 0.80)
    tracked = sum(directed_rate(row) for row in settled) / len(settled)
    tracking_error = abs(tracked - target_magnitude) / target_magnitude
    if tracking_error > 0.07:
        failures.append(f"{scenario.name}: {tracking_error * 100:.1f}% tracking error")

    cross_fields = [GYRO_FIELD[candidate] for candidate in AXES if candidate != axis]
    cross_peak = max(
        abs(math.degrees(row[field])) for row in active for field in cross_fields
    )
    cross_limit = max(5.0, 0.03 * target_magnitude)
    if cross_peak > cross_limit:
        failures.append(f"{scenario.name}: {cross_peak:.1f} deg/s cross-axis coupling")

    released = [row for row in result.rows if row["sim_time_s"] >= 0.80]
    release_threshold = max(5.0, 0.05 * target_magnitude)
    stopped = first_time(
        released,
        lambda row: abs(body_rate_deg_s(row, axis)) <= release_threshold,
    )
    release_limit = {0.25: 0.056, 0.50: 0.064, 1.00: 0.100}[scenario.magnitude]
    if stopped is None or stopped - 0.80 > release_limit + 1e-6:
        latency = "never" if stopped is None else f"{(stopped - 0.80) * 1000:.0f} ms"
        failures.append(f"{scenario.name}: release braking took {latency}")
    residual_start = 0.96 if stopped is None else stopped + 0.064
    hold = window(result.rows, residual_start, scenario.duration_s + TIME_STEP_SECONDS)
    residual = max((abs(body_rate_deg_s(row, axis)) for row in hold), default=0.0)
    if residual > 3.0:
        failures.append(f"{scenario.name}: {residual:.1f} deg/s residual rotation")

    metrics[scenario.name] = {
        "target_deg_s": round(expected_target, 3),
        "onset_ms": round(onset_latency * 1000),
        "t90_ms": round(ninety_latency * 1000),
        "overshoot_percent": round(overshoot * 100, 2),
        "tracking_error_percent": round(tracking_error * 100, 2),
        "cross_axis_peak_deg_s": round(cross_peak, 2),
        "release_ms": None if stopped is None else round((stopped - 0.80) * 1000),
        "residual_deg_s": round(residual, 2),
    }


def analyze_mixed(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    scenario = result.scenario
    settled = window(result.rows, 0.68, 0.80)
    active_commands = scenario.sequence[1]
    axis_errors: dict[str, float] = {}
    active_axes: list[str] = []
    target_magnitudes: dict[str, float] = {}
    for axis in AXES:
        stick = active_commands[AXIS_COMMAND_INDEX[axis]]
        if abs(stick) < 1e-9:
            continue
        active_axes.append(axis)
        expected_target = EXPECTED_BODY_SIGN[axis] * expected_rate(axis, stick)
        target_magnitudes[axis] = abs(expected_target)
        logged_target = sum(
            row[f"target_{axis}_rate_deg_s"] for row in settled
        ) / len(settled)
        actual = sum(body_rate_deg_s(row, axis) for row in settled) / len(settled)
        if abs(logged_target - expected_target) > 0.1:
            failures.append(f"{scenario.name}: {axis} target sign/rate is wrong")
        error = abs(actual - expected_target) / abs(expected_target)
        axis_errors[axis] = round(error * 100, 2)
        limit = 0.20 if "full_triple" in scenario.name else 0.10
        if actual * expected_target <= 0.0:
            failures.append(f"{scenario.name}: {axis} reversed under mixed input")
        elif error > limit:
            failures.append(
                f"{scenario.name}: {axis} mixed tracking error {error * 100:.1f}%"
            )

    released = [row for row in result.rows if row["sim_time_s"] >= 0.80]
    stopped = first_time(
        released,
        lambda row: all(
            abs(body_rate_deg_s(row, axis))
            <= max(5.0, 0.05 * target_magnitudes[axis])
            for axis in active_axes
        ),
    )
    if "full_triple" in scenario.name:
        release_limit = 0.160
    elif "full_pair" in scenario.name:
        release_limit = 0.130
    else:
        release_limit = 0.090
    if stopped is None or stopped - 0.80 > release_limit + 1e-6:
        latency = "never" if stopped is None else f"{(stopped - 0.80) * 1000:.0f} ms"
        failures.append(f"{scenario.name}: mixed release took {latency}")
    residual_start = 1.04 if stopped is None else stopped + 0.064
    residual_rows = window(
        result.rows, residual_start, scenario.duration_s + TIME_STEP_SECONDS
    )
    residual = max(
        (
            abs(body_rate_deg_s(row, axis))
            for row in residual_rows
            for axis in active_axes
        ),
        default=0.0,
    )
    if residual > 5.0:
        failures.append(f"{scenario.name}: mixed residual {residual:.1f} deg/s")
    metrics[scenario.name] = {
        "tracking_error_percent": axis_errors,
        "release_ms": None if stopped is None else round((stopped - 0.80) * 1000),
        "release_residual_deg_s": round(residual, 2),
    }


def analyze_throttle(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    scenario = result.scenario
    final_z = result.rows[-1]["z_m"]
    final_vz = result.rows[-1]["vz_mps"]
    delta_z = final_z - scenario.initial_altitude
    throttle = scenario.throttle or 0.0
    if scenario.name == "hover_throttle" and (abs(delta_z) > 0.15 or abs(final_vz) > 0.20):
        failures.append(f"hover_throttle: drifted {delta_z:+.2f} m, vz={final_vz:+.2f} m/s")
    elif scenario.name in ("zero_throttle", "low_throttle") and delta_z > -0.20:
        failures.append(f"{scenario.name}: did not descend ({delta_z:+.2f} m)")
    elif scenario.name == "high_throttle" and delta_z < 0.50:
        failures.append(f"high_throttle: climb too weak ({delta_z:+.2f} m)")
    elif scenario.name == "full_throttle" and delta_z < 1.00:
        failures.append(f"full_throttle: climb too weak ({delta_z:+.2f} m)")
    end_motors = motor_values(result.rows[-1])
    if max(end_motors) - min(end_motors) > 0.05:
        failures.append(f"{scenario.name}: unequal motors with centered controls")
    metrics[scenario.name] = {
        "throttle": round(throttle, 4),
        "altitude_change_m": round(delta_z, 3),
        "final_vertical_speed_m_s": round(final_vz, 3),
        "motor_speed_rad_s": round(sum(end_motors) / 4, 3),
    }


def analyze_arming(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    name = result.scenario.name
    if result.scenario.kind == "arm_reject":
        if any(row["armed"] for row in result.rows):
            failures.append(f"{name}: armed at unsafe throttle")
    else:
        after_raise = window(result.rows, 0.24, result.scenario.duration_s + 0.01)
        if not after_raise or not all(row["armed"] for row in after_raise):
            failures.append(f"{name}: did not remain armed")
        if max((max(motor_values(row)) for row in after_raise), default=0.0) <= IDLE_SPEED:
            failures.append(f"{name}: motors did not respond")
    metrics[name] = {
        "final_armed": int(result.rows[-1]["armed"]),
        "max_motor_rad_s": round(max(max(motor_values(row)) for row in result.rows), 3),
    }


def analyze_ground_safety(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    max_altitude = max(row["z_m"] for row in result.rows)
    max_motor = max(max(motor_values(row)) for row in result.rows)
    if max_altitude > 0.08:
        failures.append(f"{result.scenario.name}: launched to {max_altitude:.2f} m")
    if max_motor > IDLE_SPEED + 0.01:
        failures.append(f"{result.scenario.name}: motor exceeded idle ({max_motor:.1f})")
    metrics[result.scenario.name] = {
        "max_altitude_m": round(max_altitude, 3),
        "max_motor_rad_s": round(max_motor, 3),
    }


def analyze_reversal(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    scenario = result.scenario
    assert scenario.axis and scenario.direction
    axis = scenario.axis
    first_direction = EXPECTED_BODY_SIGN[axis] * scenario.direction
    second_direction = -first_direction
    target = abs(expected_rate(axis, 1.0))
    before = window(result.rows, 0.56, 0.60)
    initial_rate = sum(first_direction * body_rate_deg_s(row, axis) for row in before) / len(before)
    reversed_rows = window(result.rows, 0.60, 0.90)
    decreasing = first_time(
        reversed_rows,
        lambda row: first_direction * body_rate_deg_s(row, axis) < initial_rate - 1.0,
    )
    zero_cross = first_time(
        reversed_rows,
        lambda row: first_direction * body_rate_deg_s(row, axis) <= 0.0,
    )
    opposite_ninety = first_time(
        reversed_rows,
        lambda row: second_direction * body_rate_deg_s(row, axis) >= 0.90 * target,
    )
    if decreasing is None or decreasing - 0.60 > 0.0161:
        failures.append(f"{scenario.name}: did not begin braking within 16 ms")
    if zero_cross is None or zero_cross - 0.60 > 0.1001:
        failures.append(f"{scenario.name}: rate did not reverse within 100 ms")
    if opposite_ninety is None or opposite_ninety - 0.60 > 0.2001:
        failures.append(f"{scenario.name}: opposite rate did not reach 90% within 200 ms")
    released = [row for row in result.rows if row["sim_time_s"] >= 0.90]
    stopped = first_time(
        released,
        lambda row: abs(body_rate_deg_s(row, axis)) <= 0.05 * target,
    )
    if stopped is None or stopped - 0.90 > 0.1001:
        failures.append(f"{scenario.name}: failed to brake after reversal release")
    metrics[scenario.name] = {
        "braking_onset_ms": None if decreasing is None else round((decreasing - 0.60) * 1000),
        "zero_cross_ms": None if zero_cross is None else round((zero_cross - 0.60) * 1000),
        "opposite_t90_ms": None if opposite_ninety is None else round((opposite_ninety - 0.60) * 1000),
        "release_ms": None if stopped is None else round((stopped - 0.90) * 1000),
    }


def analyze_acro(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    scenario = result.scenario
    assert scenario.axis in ("roll", "pitch")
    angle_field = f"{scenario.axis}_rad"
    settled = window(result.rows, 0.72, 0.82)
    late = window(result.rows, 1.28, 1.44)
    retained = math.degrees(sum(row[angle_field] for row in settled) / len(settled))
    final = math.degrees(sum(row[angle_field] for row in late) / len(late))
    drift = abs(final - retained)
    if abs(retained) < 15.0:
        failures.append(f"{scenario.name}: retained only {abs(retained):.1f} degrees")
    if drift > 2.0 or abs(final) < 0.90 * abs(retained):
        failures.append(f"{scenario.name}: self-levelled or drifted {drift:.2f} degrees")
    metrics[scenario.name] = {
        "retained_angle_deg": round(retained, 2),
        "late_angle_deg": round(final, 2),
        "post_braking_drift_deg": round(drift, 3),
    }


def normalized_motor_thrust(speed: float) -> float:
    return (speed**2 - IDLE_SPEED**2) / (MAX_MOTOR_SPEED**2 - IDLE_SPEED**2)


def analyze_airmode(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    active = window(result.rows, 0.35, 0.70)
    max_motor = max(max(motor_values(row)) for row in active)
    collective = max(
        sum(normalized_motor_thrust(speed) for speed in motor_values(row)) / 4.0
        for row in active
    )
    hover_collective = HOVER_THROTTLE ** float(FLIGHT_PROFILE["throttle_exponent"])
    if max_motor <= IDLE_SPEED + 1.0:
        failures.append(f"{result.scenario.name}: Airmode provided no rate authority")
    if collective >= hover_collective:
        failures.append(f"{result.scenario.name}: zero throttle produced hover-or-higher collective")
    metrics[result.scenario.name] = {
        "max_motor_rad_s": round(max_motor, 3),
        "max_collective_fraction": round(collective, 4),
        "hover_collective_fraction": round(hover_collective, 4),
    }


def analyze_landed_safety(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    collisions = [row for row in result.rows if row["collision"] > 0.5]
    active = window(result.rows, 2.24, result.scenario.duration_s + 0.01)
    max_motor = max(max(motor_values(row)) for row in active)
    if not collisions:
        failures.append(f"{result.scenario.name}: landing contact was never observed")
    if max_motor > IDLE_SPEED + 0.01:
        failures.append(f"{result.scenario.name}: landed Airmode exceeded idle ({max_motor:.1f})")
    metrics[result.scenario.name] = {
        "collision_samples": len(collisions),
        "post_landing_max_motor_rad_s": round(max_motor, 3),
    }


def analyze_recovery(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    recovered = [row for row in result.rows if row["recovery_count"] >= 1.0]
    if not recovered:
        failures.append(f"{result.scenario.name}: boundary recovery never occurred")
        return
    transition_index = result.rows.index(recovered[0])
    # Supervisor field writes become visible through getPosition on the next
    # physics sample, while the recovery counter changes immediately.
    pose_row = result.rows[min(transition_index + 1, len(result.rows) - 1)]
    first = recovered[0]
    if int(max(row["recovery_count"] for row in result.rows)) != 1:
        failures.append(f"{result.scenario.name}: recovery repeated unexpectedly")
    if abs(pose_row["x_m"] + 7.0) > 0.03 or abs(pose_row["y_m"]) > 0.03 or abs(pose_row["z_m"] - 0.03) > 0.03:
        failures.append(f"{result.scenario.name}: recovery did not return to home pose")
    high_after_reset = window(result.rows, first["sim_time_s"], 1.30)
    if any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in high_after_reset):
        failures.append(f"{result.scenario.name}: high throttle rearmed after recovery")
    deflected_low = window(result.rows, 1.32, 1.50)
    if any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in deflected_low):
        failures.append(
            f"{result.scenario.name}: attitude stick survived reset into rearm"
        )
    pending = window(result.rows, 1.32, 1.48)
    if pending and not all(row["boundary_rearm_pending"] > 0.5 for row in pending):
        failures.append(f"{result.scenario.name}: boundary neutral latch cleared early")
    after_rearm = window(result.rows, 1.76, result.scenario.duration_s + 0.01)
    if not after_rearm or not all(row["armed"] > 0.5 for row in after_rearm):
        failures.append(f"{result.scenario.name}: failed to rearm after lowering throttle")
    if max((row["z_m"] for row in after_rearm), default=0.0) < 0.15:
        failures.append(f"{result.scenario.name}: failed to fly after recovery")
    metrics[result.scenario.name] = {
        "recovery_time_s": round(first["sim_time_s"], 3),
        "final_armed": int(result.rows[-1]["armed"]),
        "neutral_latch_samples": len(pending),
        "post_recovery_max_altitude_m": round(max(row["z_m"] for row in after_rearm), 3),
    }


def analyze_inverted_recovery(
    result: Result,
    failures: list[str],
    metrics: dict[str, dict],
) -> None:
    inverted = [
        row
        for row in result.rows
        if row["collision"] > 0.5 and abs(row["roll_rad"]) > 2.5
    ]
    recovered = [row for row in result.rows if row["recovery_count"] >= 1.0]
    if not inverted:
        failures.append(f"{result.scenario.name}: inverted contact was never observed")
        return
    if not recovered:
        failures.append(f"{result.scenario.name}: inverted crash was not recovered")
        return
    first = recovered[0]
    transition_index = result.rows.index(first)
    pose_row = result.rows[min(transition_index + 1, len(result.rows) - 1)]
    if int(first["last_disarm_reason_code"]) != 5:
        failures.append(f"{result.scenario.name}: wrong recovery reason code")
    if (
        abs(pose_row["x_m"] + 7.0) > 0.03
        or abs(pose_row["y_m"]) > 0.03
        or abs(pose_row["z_m"] - 0.03) > 0.03
        or abs(pose_row["roll_rad"]) > 0.05
        or abs(pose_row["pitch_rad"]) > 0.05
    ):
        failures.append(f"{result.scenario.name}: recovery was not upright at home")
    if first["armed"] > 0.5 or max(motor_values(first)) > 0.001:
        failures.append(f"{result.scenario.name}: motors remained armed during reset")
    if int(max(row["recovery_count"] for row in result.rows)) != 1:
        failures.append(f"{result.scenario.name}: recovery repeated unexpectedly")
    recovery_delay = first["sim_time_s"] - inverted[0]["sim_time_s"]
    configured_delay = float(FLIGHT_PROFILE["crash_recovery_seconds"])
    if recovery_delay < configured_delay - TIME_STEP_SECONDS:
        failures.append(f"{result.scenario.name}: recovery happened before the safety dwell")
    if recovery_delay > configured_delay + 0.20:
        failures.append(f"{result.scenario.name}: recovery took too long after the crash settled")
    if first["rearm_inhibit"] < 0.5 or first["boundary_rearm_pending"] < 0.5:
        failures.append(f"{result.scenario.name}: post-crash neutral rearm latch was missing")
    after_throttle = window(result.rows, 1.52, result.scenario.duration_s + 0.01)
    if not after_throttle or not all(row["armed"] > 0.5 for row in after_throttle):
        failures.append(f"{result.scenario.name}: failed to rearm after the crash reset")
    max_altitude = max((row["z_m"] for row in after_throttle), default=0.0)
    if max_altitude < 0.15:
        failures.append(f"{result.scenario.name}: failed to fly after the crash reset")
    metrics[result.scenario.name] = {
        "inverted_contact_samples": len(inverted),
        "recovery_time_s": round(first["sim_time_s"], 3),
        "recovery_delay_s": round(recovery_delay, 3),
        "post_recovery_max_altitude_m": round(max_altitude, 3),
        "final_armed": int(result.rows[-1]["armed"]),
    }


def analyze_crash(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    collision_time = first_time(result.rows, lambda row: row["collision"] > 0.5)
    if collision_time is None:
        failures.append(f"{result.scenario.name}: collision was never logged")
    elif result.rows[-1]["sim_time_s"] - collision_time < 0.50:
        failures.append(f"{result.scenario.name}: telemetry did not survive 0.5 s after impact")
    takeoff = window(result.rows, 1.35, result.scenario.duration_s + 0.01)
    max_altitude = max((row["z_m"] for row in takeoff), default=0.0)
    if max_altitude < 0.15:
        failures.append(f"{result.scenario.name}: could not take off after collision")
    metrics[result.scenario.name] = {
        "collision_time_s": None if collision_time is None else round(collision_time, 3),
        "post_collision_max_altitude_m": round(max_altitude, 3),
    }


def analyze_emergency(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    stopped = window(result.rows, 0.50, 0.82)
    if not stopped or any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in stopped):
        failures.append(f"{result.scenario.name}: emergency stop did not latch")
    if not all(row["explicit_arm_required"] > 0.5 for row in window(result.rows, 0.52, 0.82)):
        failures.append(f"{result.scenario.name}: explicit rearm was not required")
    rearmed = window(result.rows, 0.98, result.scenario.duration_s + 0.01)
    if not rearmed or not all(row["armed"] > 0.5 for row in rearmed):
        failures.append(f"{result.scenario.name}: did not rearm after explicit arm at low throttle")
    if max((max(motor_values(row)) for row in rearmed), default=0.0) <= IDLE_SPEED:
        failures.append(f"{result.scenario.name}: motors did not restart")
    metrics[result.scenario.name] = {
        "stopped_samples": len(stopped),
        "final_armed": int(result.rows[-1]["armed"]),
        "disarm_events": int(result.rows[-1]["disarm_event_count"]),
    }


def analyze_low_emergency(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    latched = window(result.rows, 0.20, 0.85)
    if not latched or any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in latched):
        failures.append(f"{result.scenario.name}: low-throttle stop silently rearmed")
    if not all(row["explicit_arm_required"] > 0.5 for row in window(result.rows, 0.24, 0.85)):
        failures.append(f"{result.scenario.name}: explicit-arm latch did not persist")
    after_explicit = window(result.rows, 0.88, result.scenario.duration_s + 0.01)
    if not after_explicit or not all(row["armed"] > 0.5 for row in after_explicit):
        failures.append(f"{result.scenario.name}: explicit low-throttle arm failed")
    metrics[result.scenario.name] = {
        "latched_samples": len(latched),
        "final_armed": int(result.rows[-1]["armed"]),
        "disarm_events": int(result.rows[-1]["disarm_event_count"]),
    }


def analyze_rejected_emergency_arm(
    result: Result,
    failures: list[str],
    metrics: dict[str, dict],
) -> None:
    after_stop = window(result.rows, 0.50, result.scenario.duration_s + 0.01)
    if not after_stop or any(
        row["armed"] > 0.5 or max(motor_values(row)) > 0.001
        for row in after_stop
    ):
        failures.append(
            f"{result.scenario.name}: invalid E press was queued and rearmed later"
        )
    latch_window = window(result.rows, 0.54, result.scenario.duration_s + 0.01)
    if not latch_window or not all(
        row["explicit_arm_required"] > 0.5 for row in latch_window
    ):
        failures.append(
            f"{result.scenario.name}: explicit-arm latch did not remain active"
        )
    metrics[result.scenario.name] = {
        "latched_samples": len(after_stop),
        "final_armed": int(result.rows[-1]["armed"]),
        "disarm_events": int(result.rows[-1]["disarm_event_count"]),
    }


def analyze_input_loss(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    failed_period = window(result.rows, 0.50, 0.75)
    if not failed_period or any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in failed_period):
        failures.append(f"{result.scenario.name}: motors stayed active after input loss")
    if not any(row["last_disarm_reason_code"] == 2.0 for row in result.rows):
        failures.append(f"{result.scenario.name}: input-loss reason was not persisted")
    rearmed = window(result.rows, 0.78, result.scenario.duration_s + 0.01)
    if not rearmed or not all(row["armed"] > 0.5 for row in rearmed):
        failures.append(f"{result.scenario.name}: fresh low source did not rearm")
    metrics[result.scenario.name] = {
        "failed_samples": len(failed_period),
        "final_armed": int(result.rows[-1]["armed"]),
        "disarm_events": int(result.rows[-1]["disarm_event_count"]),
    }


def analyze_low_input_loss(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    held_low = window(result.rows, 0.20, 0.72)
    if not held_low or any(row["armed"] > 0.5 or max(motor_values(row)) > 0.001 for row in held_low):
        failures.append(f"{result.scenario.name}: held-low input loss silently rearmed")
    if not all(row["rearm_inhibit"] > 0.5 for row in window(result.rows, 0.24, 0.72)):
        failures.append(f"{result.scenario.name}: rearm gesture latch did not persist")
    rearmed = window(result.rows, 0.88, result.scenario.duration_s + 0.01)
    if not rearmed or not all(row["armed"] > 0.5 for row in rearmed):
        failures.append(f"{result.scenario.name}: high-to-low gesture did not rearm")
    metrics[result.scenario.name] = {
        "held_low_samples": len(held_low),
        "final_armed": int(result.rows[-1]["armed"]),
        "disarm_events": int(result.rows[-1]["disarm_event_count"]),
    }


def analyze_stress(result: Result, failures: list[str], metrics: dict[str, dict]) -> None:
    max_rate = max(
        abs(body_rate_deg_s(row, axis)) for row in result.rows for axis in AXES
    )
    if max_rate > 850.0:
        failures.append(f"{result.scenario.name}: body rate escaped to {max_rate:.1f} deg/s")
    metrics[result.scenario.name] = {
        "telemetry_rows": len(result.rows),
        "max_abs_body_rate_deg_s": round(max_rate, 2),
    }


def check_throttle_invariance(metrics: dict[str, dict], failures: list[str]) -> None:
    for axis in AXES:
        names = [
            f"{axis}_0.25_pos_low_throttle",
            f"{axis}_0.25_pos",
            f"{axis}_0.25_pos_high_throttle",
        ]
        if not all(name in metrics for name in names):
            continue
        times = [metrics[name]["t90_ms"] for name in names]
        tracking = [metrics[name]["tracking_error_percent"] for name in names]
        if max(times) - min(times) > 16:
            failures.append(f"throttle_invariance_{axis}: t90 spread was {max(times) - min(times)} ms")
        if max(tracking) - min(tracking) > 2.0:
            failures.append(f"throttle_invariance_{axis}: tracking changed across throttle")


def check_throttle_monotonic(metrics: dict[str, dict], failures: list[str]) -> None:
    names = ("zero_throttle", "low_throttle", "hover_throttle", "high_throttle", "full_throttle")
    if not all(name in metrics for name in names):
        return
    speeds = [metrics[name]["motor_speed_rad_s"] for name in names]
    if any(second <= first for first, second in zip(speeds, speeds[1:])):
        failures.append("throttle_curve: motor output was not strictly monotonic")


def check_reproducibility(metrics: dict[str, dict], failures: list[str]) -> None:
    names = ("determinism_replay_a", "determinism_replay_b")
    if not all(name in metrics for name in names):
        return
    first, second = (metrics[name] for name in names)
    if abs(first["t90_ms"] - second["t90_ms"]) > 8:
        failures.append("determinism: response timing changed by more than one step")
    if abs(first["tracking_error_percent"] - second["tracking_error_percent"]) > 1.0:
        failures.append("determinism: settled response changed by more than 1%")


def main() -> int:
    if not WEBOTS.is_file():
        print(f"Webots was not found at {WEBOTS}", file=sys.stderr)
        return 2
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = PROJECT_ROOT / "logs" / "validation" / timestamp
    run_root.mkdir(parents=True, exist_ok=False)
    scenarios = build_scenarios()
    failures: list[str] = []
    metrics: dict[str, dict] = {}
    results: list[Result] = []

    # LaunchServices rejects bursts of concurrent hidden app launches on macOS.
    # Run one hidden instance at a time; this also makes physics comparisons stable.
    with ThreadPoolExecutor(max_workers=1) as executor:
        futures = {
            executor.submit(run_scenario, scenario, index, run_root): scenario
            for index, scenario in enumerate(scenarios)
        }
        for future in as_completed(futures):
            scenario = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                failures.append(f"{scenario.name}: {error}")

    for result in sorted(results, key=lambda item: item.scenario.name):
        if not assert_common(result, failures):
            continue
        try:
            kind = result.scenario.kind
            if kind == "axis":
                analyze_axis(result, failures, metrics)
            elif kind == "mixed":
                analyze_mixed(result, failures, metrics)
            elif kind == "throttle":
                analyze_throttle(result, failures, metrics)
            elif kind in ("arm_reject", "arm_accept"):
                analyze_arming(result, failures, metrics)
            elif kind == "ground_safety":
                analyze_ground_safety(result, failures, metrics)
            elif kind == "reversal":
                analyze_reversal(result, failures, metrics)
            elif kind == "acro":
                analyze_acro(result, failures, metrics)
            elif kind == "airmode":
                analyze_airmode(result, failures, metrics)
            elif kind == "landed_safety":
                analyze_landed_safety(result, failures, metrics)
            elif kind == "recovery":
                analyze_recovery(result, failures, metrics)
            elif kind == "inverted_recovery":
                analyze_inverted_recovery(result, failures, metrics)
            elif kind == "crash":
                analyze_crash(result, failures, metrics)
            elif kind == "emergency":
                analyze_emergency(result, failures, metrics)
            elif kind == "emergency_low":
                analyze_low_emergency(result, failures, metrics)
            elif kind == "emergency_reject":
                analyze_rejected_emergency_arm(result, failures, metrics)
            elif kind == "input_loss":
                analyze_input_loss(result, failures, metrics)
            elif kind == "input_loss_low":
                analyze_low_input_loss(result, failures, metrics)
            elif kind == "stress":
                analyze_stress(result, failures, metrics)
            else:
                failures.append(
                    f"{result.scenario.name}: unknown analyzer kind {kind}"
                )
        except Exception as error:
            failures.append(f"{result.scenario.name}: analyzer error: {error}")
    check_throttle_invariance(metrics, failures)
    check_throttle_monotonic(metrics, failures)
    check_reproducibility(metrics, failures)

    apparatus_records = {
        (
            result.run_manifest["apparatus"]["id"],
            result.run_manifest["apparatus"]["version"],
            result.run_manifest["apparatus"]["manifest_sha256"],
        )
        for result in results
    }
    if len(apparatus_records) > 1:
        failures.append("validation scenarios used different apparatus revisions")
    apparatus_record = None
    if apparatus_records:
        apparatus_id, apparatus_version, manifest_sha256 = next(
            iter(apparatus_records)
        )
        apparatus_record = {
            "id": apparatus_id,
            "version": apparatus_version,
            "manifest_sha256": manifest_sha256,
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apparatus": apparatus_record,
        "scenario_count": len(scenarios),
        "completed_scenario_count": len(results),
        "passed": not failures,
        "failures": failures,
        "run_fingerprints": {
            result.scenario.name: result.run_manifest["run_fingerprint_sha256"]
            for result in sorted(results, key=lambda item: item.scenario.name)
        },
        "metrics": metrics,
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(
        f"Flight-dynamics validation: {len(scenarios)} scenarios, "
        f"{len(failures)} failed checks"
    )
    print(f"Report: {report_path}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(
        "PASS: axes, directions, rates, release, reversal, Acro retention, mixing, "
        "throttle, arming, Airmode, landing, recovery, collision, stop/rearm, and stress"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
