"""Minimal manual-flight and synchronized-sensor controller for Webots."""

from array import array
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from math import radians
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

from controller import Keyboard, Supervisor
import hid

from input_mapping import (
    betaflight_rate_setpoint,
    clamp,
    hid_byte_to_signed,
    normalize_axis,
    normalize_throttle,
    shape_throttle,
    thrust_mix_to_motor_speeds,
)
from apparatus import (
    canonical_sha256,
    classify_course_zone,
    file_sha256,
    load_and_verify_apparatus,
)
from outage_baselines import (
    OutageConfigurationError,
    OutageRuntime,
    OutageRuntimeError,
    PilotFrameSelector,
    materialize_schedule,
    validate_zone_references,
)
from outage_artifacts import write_bgra_png, write_depth_bundle


VALIDATION_QUEUE_ENV = "DREAM_MODE_VALIDATION_QUEUE_FILE"


def configure_single_session_validation() -> tuple[Path, int, str, int] | None:
    """Load one validation scenario before the Webots controller is initialized."""
    configured = os.environ.get(VALIDATION_QUEUE_ENV)
    if not configured:
        return None
    queue_path = Path(configured).expanduser().resolve()
    with queue_path.open(encoding="utf-8") as source:
        queue = json.load(source)
    if queue.get("schema_version") != 1:
        raise ValueError("Validation queue schema_version must be 1")
    scenarios = queue.get("scenarios")
    next_index = queue.get("next_index")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Validation queue scenarios must be a non-empty list")
    if type(next_index) is not int or not 0 <= next_index < len(scenarios):
        raise ValueError("Validation queue next_index is out of range")
    scenario = scenarios[next_index]
    if not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str):
        raise ValueError("Validation queue scenario name is invalid")
    overrides = scenario.get("environment")
    if not isinstance(overrides, dict) or not overrides:
        raise ValueError("Validation queue scenario environment is invalid")
    for name, value in overrides.items():
        if (
            not isinstance(name, str)
            or not name.startswith("DREAM_MODE_")
            or name == VALIDATION_QUEUE_ENV
            or not isinstance(value, str)
        ):
            raise ValueError("Validation queue contains an invalid environment override")
        os.environ[name] = value
    return queue_path, next_index, scenario["name"], len(scenarios)


class DreamModeController(Supervisor):
    LOG_PERIOD_SECONDS = 0.04
    TELEMETRY_FLUSH_PERIOD_SECONDS = 0.5
    CAMERA_PERIOD_MS = 16
    DEPTH_PERIOD_MS = 64
    MAX_RAW_AXES = 8

    def __init__(
        self,
        validation_queue: tuple[Path, int, str, int] | None = None,
    ) -> None:
        super().__init__()
        self.validation_queue = validation_queue
        self.time_step = int(self.getBasicTimeStep())
        webots_project_root = Path(self.getProjectPath()).resolve()
        source_project_root = Path(__file__).resolve().parents[2]
        self.project_root = (
            webots_project_root
            if (webots_project_root / "config" / "controller.json").is_file()
            else source_project_root
        )
        self.config = self._load_config()
        self.flight_profile = self.config["flight_profile"]
        (
            self.apparatus,
            self.course_zones,
            self.locked_file_hashes,
            self.apparatus_manifest_sha256,
        ) = load_and_verify_apparatus(self.project_root, self.config)
        self.run_seed = int(self.apparatus["random_seed"])
        self.live_random_seed = self._verify_live_random_seed()
        self.controller_config_sha256 = self.locked_file_hashes[
            "config/controller.json"
        ]
        self.webots_actual_version = self._read_webots_version()
        self.outage_config_path = (
            self.project_root / self.config["outage_baseline_config"]
        ).resolve()
        try:
            with self.outage_config_path.open(encoding="utf-8") as outage_file:
                self.outage_config = json.load(outage_file)
            self.outage_mode = os.environ.get(
                "DREAM_MODE_OUTAGE_MODE", "off"
            ).strip().lower()
            condition_override = os.environ.get("DREAM_MODE_OUTAGE_CONDITION")
            self.outage_condition_override = (
                None
                if condition_override in (None, "", "configured")
                else condition_override.strip().lower()
            )
            seed_override_text = os.environ.get("DREAM_MODE_OUTAGE_SEED")
            seed_override = (
                None if seed_override_text is None else int(seed_override_text)
            )
            script_text = os.environ.get("DREAM_MODE_OUTAGE_SCRIPT")
            scripted_events = None if script_text is None else json.loads(script_text)
            self.outage_schedule = materialize_schedule(
                self.outage_config,
                self.outage_mode,
                condition_override=self.outage_condition_override,
                seed_override=seed_override,
                scripted_events=scripted_events,
            )
            validate_zone_references(
                self.outage_config,
                self.outage_schedule,
                self.course_zones,
            )
            self.research_overlay_safety = self._verify_research_overlay_safety(
                self.outage_mode
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid Phase 2 outage configuration: {error}") from error
        outage_timing = self.outage_config["timing"]
        if int(outage_timing["control_step_ms"]) != self.time_step:
            raise RuntimeError(
                "Outage control step does not match the Webots basic time step"
            )
        if int(outage_timing["display_period_ms"]) != self.CAMERA_PERIOD_MS:
            raise RuntimeError("Outage display period does not match the RGB camera")
        if int(outage_timing["rgbd_anchor_period_ms"]) != self.DEPTH_PERIOD_MS:
            raise RuntimeError("Outage RGB-D anchor period does not match depth")
        self.outage_config_sha256 = file_sha256(self.outage_config_path)
        self.outage_runtime = OutageRuntime(self.outage_schedule)
        self.apparatus_version_parts = tuple(
            int(part) for part in self.apparatus["version"].split(".")
        )
        self.run_dir = self._create_run_directory()
        self.log_period_seconds = max(
            self.time_step / 1000.0,
            float(
                os.environ.get(
                    "DREAM_MODE_LOG_PERIOD_SECONDS",
                    self.LOG_PERIOD_SECONDS,
                )
            ),
        )

        # Keep the pilot feed near 60 FPS while sampling the more expensive
        # research depth stream at a lower rate. Flight control remains at the
        # world's 8 ms basic time step.
        self.camera = self._enable_device(
            "research camera", self.CAMERA_PERIOD_MS
        )
        self.pilot_camera = self._enable_device(
            "pilot analog camera", self.CAMERA_PERIOD_MS
        )
        self.depth = self._enable_device("depth", self.DEPTH_PERIOD_MS)
        self.continuous_research_sensors = (
            os.environ.get("DREAM_MODE_CONTINUOUS_SENSORS") == "1"
            or bool(self.config.get("continuous_research_sensors", False))
            or self.outage_mode != "off"
        )
        self.capture_initial_frames_enabled = (
            os.environ.get("DREAM_MODE_SKIP_INITIAL_CAPTURE") != "1"
        )
        self.pilot_display = self.getDevice("pilot display")
        if self.pilot_display is None:
            raise RuntimeError('Required Webots device "pilot display" is missing')
        if (
            self.pilot_display.getWidth() != self.pilot_camera.getWidth()
            or self.pilot_display.getHeight() != self.pilot_camera.getHeight()
        ):
            raise RuntimeError(
                "Pilot display dimensions must match the analog pilot camera"
            )
        self.pilot_display_node = self.getFromDevice(self.pilot_display._tag)
        camera_node = self.getFromDevice(self.camera._tag)
        pilot_camera_node = self.getFromDevice(self.pilot_camera._tag)
        depth_node = self.getFromDevice(self.depth._tag)
        if (
            self.pilot_display_node is None
            or camera_node is None
            or pilot_camera_node is None
            or depth_node is None
        ):
            raise RuntimeError("Could not resolve Phase 2 rendering devices")
        # The pilot display sits directly in front of the mounted Viewpoint.
        # Hide that surface from both research sensors to avoid recursively
        # capturing the display instead of the simulated world.
        self.pilot_display_node.setVisibility(camera_node, False)
        self.pilot_display_node.setVisibility(pilot_camera_node, False)
        self.pilot_display_node.setVisibility(depth_node, False)
        self.pilot_display.setAlpha(0.0)
        self.pilot_display.setOpacity(1.0)
        self.pilot_display.fillRectangle(
            0,
            0,
            self.pilot_display.getWidth(),
            self.pilot_display.getHeight(),
        )
        self.pilot_display.attachCamera(self.pilot_camera)
        self.pilot_frame_selector = PilotFrameSelector(
            self.pilot_camera.getWidth() * self.pilot_camera.getHeight() * 4
        )
        self.pilot_frame_id = 0
        self.last_pilot_live_frame_id = None
        self.last_pilot_live_frame_time = None
        self.last_pilot_live_frame = None
        self.outage_anchor_frame_id = None
        self.outage_anchor_frame_time = None
        self.outage_anchor_sha256 = None
        self.outage_display_image = None
        self.outage_artifact_dir = None
        self.outage_artifact_records = []
        self.outage_current_artifact = None
        self.outage_pending_return_captures = []
        self.outage_artifacts_flushed = False
        self.display_readback_validation_enabled = (
            os.environ.get("DREAM_MODE_VALIDATE_DISPLAY_READBACK") == "1"
        )
        self.viewpoint_capture_validation_enabled = (
            os.environ.get("DREAM_MODE_CAPTURE_VIEWPOINT") == "1"
        )
        self.outage_midpoint_saved = False
        self.last_outage_transition = None
        self.outage_events_file = None
        self.display_frames_file = None
        self.display_frames = None
        self.last_display_flush_wall_time = time.monotonic()
        self.display_frame_logging_enabled = (
            self.outage_mode != "off"
            or os.environ.get("DREAM_MODE_LOG_DISPLAY_FRAMES") == "1"
        )
        self.imu = self._enable_device("inertial_unit")
        self.gps = self._enable_device("gps")
        self.gyro = self._enable_device("gyro")
        self.accelerometer = self._enable_device("accelerometer")

        self.keyboard = self.getKeyboard()
        self.keyboard.enable(self.time_step)
        self.joystick = None
        self.joystick_name = ""
        self.joystick_report = []
        self.joystick_report_wall_time = None
        self.joystick_open_wall_time = None
        self.joystick_malformed_since = None
        self.joystick_path = None
        self.joystick_identity = None
        self.last_failed_joystick_path = None
        self.last_joystick_scan_time = -1.0
        self.joystick_enabled = os.environ.get("DREAM_MODE_DISABLE_JOYSTICK") != "1"
        self.current_input_source = "invalid"
        self.input_can_arm = False
        self.armed_input_source = None
        self.rearm_inhibit = False
        self.rearm_source_required = None
        self.rearm_high_seen = False
        self.boundary_rearm_pending = False
        self.boundary_neutral_since = None
        self.explicit_arm_required = False
        self.manual_disarm_requested = False
        self.manual_arm_requested = False
        self.disarm_event_count = 0
        self.last_disarm_reason_code = 0

        self.motors = {
            "front_left": self.getDevice("m4_motor"),
            "front_right": self.getDevice("m1_motor"),
            "rear_left": self.getDevice("m3_motor"),
            "rear_right": self.getDevice("m2_motor"),
        }
        for motor in self.motors.values():
            motor.setPosition(float("inf"))
            motor.setVelocity(0.0)

        self.drone_node = self.getSelf()
        initial_altitude = os.environ.get("DREAM_MODE_INITIAL_ALTITUDE")
        if initial_altitude is not None:
            translation = list(self.drone_node.getField("translation").getSFVec3f())
            translation[2] = float(initial_altitude)
            self.drone_node.getField("translation").setSFVec3f(translation)
            self.drone_node.resetPhysics()
        self.home_translation = list(
            self.drone_node.getField("translation").getSFVec3f()
        )
        self.home_rotation = list(
            self.drone_node.getField("rotation").getSFRotation()
        )
        self.viewpoint_node = self.getFromDef("FPV_VIEW")
        self.home_viewpoint_position = None
        self.home_viewpoint_orientation = None
        self.home_viewpoint_follow = None
        self.home_viewpoint_follow_type = None
        self.home_viewpoint_follow_smoothness = None
        self.home_viewpoint_field_of_view = None
        if self.viewpoint_node is not None:
            self.home_viewpoint_position = list(
                self.viewpoint_node.getField("position").getSFVec3f()
            )
            self.home_viewpoint_orientation = list(
                self.viewpoint_node.getField("orientation").getSFRotation()
            )
            self.home_viewpoint_follow = (
                self.viewpoint_node.getField("follow").getSFString()
            )
            self.home_viewpoint_follow_type = (
                self.viewpoint_node.getField("followType").getSFString()
            )
            self.home_viewpoint_follow_smoothness = (
                self.viewpoint_node.getField("followSmoothness").getSFFloat()
            )
            self.home_viewpoint_field_of_view = (
                self.viewpoint_node.getField("fieldOfView").getSFFloat()
            )
        self.recovery_enabled = os.environ.get("DREAM_MODE_DISABLE_RECOVERY") != "1"
        self.recovery_count = 0
        self.inverted_since = None
        self.airborne = False
        self.ground_contact_since = None
        self.throttle = 0.0
        self.last_log_time = -self.log_period_seconds
        self.last_telemetry_flush_wall_time = time.monotonic()
        self.captured_initial_frames = False
        self.step_count = 0
        self.smoke_test_steps = int(os.environ.get("DREAM_MODE_SMOKE_STEPS", "0"))
        self.test_emergency_stop_time = self._optional_float_env(
            "DREAM_MODE_EMERGENCY_STOP_TIME"
        )
        self.test_emergency_stop_done = False
        self.test_explicit_arm_time = self._optional_float_env(
            "DREAM_MODE_EXPLICIT_ARM_TIME"
        )
        self.test_explicit_arm_done = False
        self.test_input_loss_time = self._optional_float_env(
            "DREAM_MODE_TEST_INPUT_LOSS_TIME"
        )
        self.test_input_loss_done = False
        self.test_inverted_crash_step = int(
            os.environ.get("DREAM_MODE_INVERTED_CRASH_STEP", "0")
        )
        self.rate_impulse_step = int(os.environ.get("DREAM_MODE_RATE_IMPULSE_STEP", "0"))
        self.initial_rate_impulse = tuple(
            float(os.environ.get(f"DREAM_MODE_INITIAL_{axis.upper()}_RATE", "0"))
            for axis in ("roll", "pitch", "yaw")
        )
        self.armed = os.environ.get("DREAM_MODE_BYPASS_ARMING") == "1"
        self.last_rate_setpoints_deg_s = (0.0, 0.0, 0.0)
        self.last_motor_speeds = (0.0, 0.0, 0.0, 0.0)
        self.last_commanded_thrust = 0.0
        self.last_gyro_values = (0.0, 0.0, 0.0)
        self.rate_integral = [0.0, 0.0, 0.0]
        self.fixed_commands = tuple(
            self._optional_float_env(f"DREAM_MODE_FIXED_{axis.upper()}")
            for axis in ("roll", "pitch", "yaw", "throttle")
        )
        self.test_command_sequence = self._load_test_command_sequence()

        self.telemetry_segment_bytes = int(
            os.environ.get(
                "DREAM_MODE_TELEMETRY_SEGMENT_BYTES",
                self.config.get("telemetry_segment_bytes", 64 * 1024 * 1024),
            )
        )
        self.telemetry_max_segments = int(
            os.environ.get(
                "DREAM_MODE_TELEMETRY_MAX_SEGMENTS",
                self.config.get("telemetry_max_segments", 4),
            )
        )
        if self.telemetry_segment_bytes <= 0:
            raise ValueError("telemetry_segment_bytes must be positive")
        if self.telemetry_max_segments <= 0:
            raise ValueError("telemetry_max_segments must be positive")
        self.telemetry_segment_index = 0
        self.telemetry_file = None
        self.telemetry = None
        # Capture a controller that is already connected at process start so
        # the run manifest does not misleadingly report a null start device.
        self._refresh_joystick()
        self.run_fingerprint_sha256 = self._build_run_fingerprint()
        self._write_run_manifest()
        self._open_outage_logs()
        self._open_telemetry_segment()

        print(f"Dream Mode log directory: {self.run_dir}")
        if self.outage_mode == "off":
            print("Phase 2 outage emulator is off; pilot display is live.")
        else:
            print(
                "Phase 2 outage emulator active: "
                f"{self.outage_mode}, {len(self.outage_schedule['events'])} events."
            )
        print("Waiting for joystick; keyboard fallback is active.")
        if not self.armed:
            print("Move throttle fully down once to arm the simulated motors.")

    def _load_config(self) -> dict:
        path = self.project_root / "config" / "controller.json"
        with path.open(encoding="utf-8") as config_file:
            return json.load(config_file)

    def _advance_validation_queue(self) -> bool:
        """Persist completion and report whether another scenario remains."""
        if self.validation_queue is None:
            return False
        queue_path, scenario_index, scenario_name, scenario_count = (
            self.validation_queue
        )
        with queue_path.open(encoding="utf-8") as source:
            queue = json.load(source)
        if queue.get("next_index") != scenario_index:
            raise RuntimeError("Validation queue position changed during a scenario")
        scenarios = queue.get("scenarios", [])
        if (
            len(scenarios) != scenario_count
            or scenarios[scenario_index].get("name") != scenario_name
        ):
            raise RuntimeError("Validation queue contents changed during a scenario")
        completed = queue.setdefault("completed", [])
        if not isinstance(completed, list) or scenario_name in completed:
            raise RuntimeError("Validation queue completion history is invalid")
        completed.append(scenario_name)
        queue["next_index"] = scenario_index + 1
        temporary = queue_path.with_name(
            f".{queue_path.name}.{os.getpid()}.tmp"
        )
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(queue, output, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
        temporary.replace(queue_path)
        return queue["next_index"] < scenario_count

    def _verify_research_overlay_safety(self, outage_mode: str) -> dict:
        """Reject an experiment if a live hidden-truth overlay is visible."""
        project_path = self.project_root / "worlds" / ".dream_mode_research.wbproj"
        state = {
            "project_file": str(project_path.relative_to(self.project_root)),
            "research_camera_visible": None,
            "pilot_camera_visible": None,
            "depth_visible": None,
        }
        try:
            project_text = project_path.read_text(encoding="utf-8")
        except OSError as error:
            if outage_mode != "off":
                raise RuntimeError(
                    f"Cannot verify research overlay safety: {error}"
                ) from error
            return state
        for device, key in (
            ("research camera", "research_camera_visible"),
            ("pilot analog camera", "pilot_camera_visible"),
            ("depth", "depth_visible"),
        ):
            match = re.search(
                rf"^renderingDevicePerspectives: Dream Mode Drone:"
                rf"{re.escape(device)};([01]);",
                project_text,
                re.MULTILINE,
            )
            if match is not None:
                state[key] = match.group(1) == "1"
        unsafe = [
            label
            for label, key in (
                ("research camera", "research_camera_visible"),
                ("pilot analog camera", "pilot_camera_visible"),
                ("depth", "depth_visible"),
            )
            if state[key] is not False
        ]
        if outage_mode != "off" and unsafe:
            raise RuntimeError(
                "Phase 2 requires hidden live RGB/depth overlays; unsafe or "
                "unverified: " + ", ".join(unsafe)
            )
        return state

    @staticmethod
    def _optional_float_env(name: str) -> float | None:
        value = os.environ.get(name)
        return float(value) if value is not None else None

    def _verify_live_random_seed(self) -> int:
        world_info = self.getFromDef("WORLD_INFO")
        if world_info is None:
            raise RuntimeError('Required Webots node "WORLD_INFO" is missing')
        seed_field = world_info.getField("randomSeed")
        if seed_field is None:
            raise RuntimeError('WORLD_INFO field "randomSeed" is missing')
        live_seed = int(seed_field.getSFInt32())
        if live_seed != self.run_seed:
            raise RuntimeError(
                "Live WorldInfo randomSeed does not match the frozen apparatus: "
                f"{live_seed} != {self.run_seed}"
            )
        return live_seed

    @staticmethod
    def _read_webots_version() -> str | None:
        candidates = []
        webots_home = os.environ.get("WEBOTS_HOME")
        if webots_home:
            candidates.extend(
                [
                    Path(webots_home) / "resources" / "version.txt",
                    Path(webots_home) / "Resources" / "version.txt",
                ]
            )
        candidates.append(
            Path("/Applications/Webots.app/Contents/Resources/version.txt")
        )
        for path in candidates:
            try:
                version = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if version:
                return version
        return None

    @staticmethod
    def _load_test_command_sequence() -> list[tuple[float, tuple[float, ...]]]:
        encoded = os.environ.get("DREAM_MODE_COMMAND_SEQUENCE")
        if not encoded:
            return []
        raw_sequence = json.loads(encoded)
        if not isinstance(raw_sequence, list) or not raw_sequence:
            raise ValueError("DREAM_MODE_COMMAND_SEQUENCE must be a non-empty list")

        sequence = []
        previous_time = -1.0
        for point in raw_sequence:
            if not isinstance(point, list) or len(point) != 5:
                raise ValueError(
                    "Each command point must be [time, roll, pitch, yaw, throttle]"
                )
            time_s, roll, pitch, yaw, throttle = map(float, point)
            if time_s < previous_time or time_s < 0.0:
                raise ValueError("Command times must be non-negative and ordered")
            if any(abs(value) > 1.0 for value in (roll, pitch, yaw)):
                raise ValueError("Scripted roll, pitch, and yaw must be in [-1, 1]")
            if not 0.0 <= throttle <= 1.0:
                raise ValueError("Scripted throttle must be in [0, 1]")
            sequence.append((time_s, (roll, pitch, yaw, throttle)))
            previous_time = time_s
        return sequence

    def _scripted_commands(self) -> tuple[float, ...] | None:
        if not self.test_command_sequence:
            return None
        commands = self.test_command_sequence[0][1]
        for time_s, candidate in self.test_command_sequence:
            if self.getTime() + 1e-9 < time_s:
                break
            commands = candidate
        return commands

    def _create_run_directory(self) -> Path:
        configured = os.environ.get("DREAM_MODE_LOG_DIR")
        if configured:
            run_dir = Path(configured).expanduser().resolve()
        else:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_dir = self.project_root / "logs" / timestamp
        run_dir.mkdir(parents=True, exist_ok=bool(configured))
        controller_artifacts = (
            "run_manifest.json",
            "input_device.json",
            "input_device_events.jsonl",
            "telemetry.csv",
            "rgb_initial.png",
            "depth_initial.png",
            "depth_initial.f32",
            "depth_initial.json",
            "outage_schedule.json",
            "outage_events.jsonl",
            "display_frames.csv",
            "smoke_test_ok.json",
        )
        existing = [name for name in controller_artifacts if (run_dir / name).exists()]
        existing.extend(
            path.name
            for path in sorted(run_dir.glob("telemetry*.csv"))
            if path.name not in existing
        )
        if existing:
            raise FileExistsError(
                f"Refusing to overwrite existing run artifacts in {run_dir}: "
                + ", ".join(existing)
            )
        return run_dir

    def _dream_mode_overrides(self) -> dict[str, str]:
        return {
            name: value
            for name, value in sorted(os.environ.items())
            if name.startswith("DREAM_MODE_") and name != "DREAM_MODE_LOG_DIR"
        }

    def _git_state(self) -> dict:
        state = {"commit": None, "dirty": None}
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=self.project_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            state["commit"] = revision.stdout.strip()
            state["dirty"] = bool(status.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        return state

    def _build_run_fingerprint(self) -> str:
        payload = {
            "apparatus_id": self.apparatus["apparatus_id"],
            "apparatus_version": self.apparatus["version"],
            "apparatus_manifest_sha256": self.apparatus_manifest_sha256,
            "locked_file_hashes": self.locked_file_hashes,
            "random_seed": self.run_seed,
            "basic_time_step_ms": self.time_step,
            "telemetry_period_s": self.log_period_seconds,
            "rgb_period_ms": self.CAMERA_PERIOD_MS,
            "depth_period_ms": self.DEPTH_PERIOD_MS,
            "continuous_research_sensors": self.continuous_research_sensors,
            "recovery_enabled": self.recovery_enabled,
            "home_translation_m": self.home_translation,
            "home_rotation_axis_angle": self.home_rotation,
            "controller_config_sha256": self.controller_config_sha256,
            "outage_baseline": {
                "mode": getattr(self, "outage_mode", "off"),
                "condition_override": getattr(
                    self, "outage_condition_override", None
                ),
                "config_sha256": getattr(self, "outage_config_sha256", None),
                "schedule_sha256": getattr(self, "outage_schedule", {}).get(
                    "schedule_sha256"
                ),
                "seed": getattr(self, "outage_schedule", {}).get("seed"),
                "research_overlay_safety": getattr(
                    self, "research_overlay_safety", None
                ),
            },
            "webots_actual_version": self.webots_actual_version,
            "environment_overrides": self._dream_mode_overrides(),
        }
        return canonical_sha256(payload)

    def _write_run_manifest(self) -> None:
        manifest = {
            "schema_version": 1,
            "run_id": self.run_dir.name,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "apparatus": {
                "id": self.apparatus["apparatus_id"],
                "version": self.apparatus["version"],
                "status": self.apparatus["status"],
                "manifest_sha256": self.apparatus_manifest_sha256,
                "locked_file_hashes": self.locked_file_hashes,
            },
            "run_fingerprint_sha256": self.run_fingerprint_sha256,
            "random_seed": self.run_seed,
            "controller_config_sha256": self.controller_config_sha256,
            "runtime": {
                "webots_tested_version": self.apparatus["simulator"]["tested_version"],
                "webots_actual_version": self.webots_actual_version,
                "python_version": platform.python_version(),
                "python_executable": sys.executable,
                "basic_time_step_ms": self.time_step,
                "telemetry_period_s": self.log_period_seconds,
                "rgb_period_ms": self.CAMERA_PERIOD_MS,
                "depth_period_ms": self.DEPTH_PERIOD_MS,
                "continuous_research_sensors": self.continuous_research_sensors,
                "recovery_enabled": self.recovery_enabled,
                "home_translation_m": self.home_translation,
                "home_rotation_axis_angle": self.home_rotation,
            },
            "environment_overrides": self._dream_mode_overrides(),
            "outage_baseline": {
                "experiment_id": self.outage_schedule["experiment_id"],
                "experiment_version": self.outage_schedule["experiment_version"],
                "mode": self.outage_mode,
                "condition_override": self.outage_condition_override,
                "seed": self.outage_schedule["seed"],
                "config_file": self.config["outage_baseline_config"],
                "config_sha256": self.outage_config_sha256,
                "schedule_file": "outage_schedule.json",
                "schedule_sha256": self.outage_schedule["schedule_sha256"],
                "pilot_display": {
                    "device": "pilot display",
                    "source_camera": "pilot analog camera",
                    "width": self.pilot_display.getWidth(),
                    "height": self.pilot_display.getHeight(),
                    "period_ms": self.CAMERA_PERIOD_MS,
                    "command_latency_control_steps": 1,
                },
                "artifact_write_policy": "capture in memory, write after flight",
                "physical_display_readback": self.display_readback_validation_enabled,
                "viewpoint_capture": self.viewpoint_capture_validation_enabled,
                "research_overlay_safety": self.research_overlay_safety,
            },
            "input_device_at_start": self.joystick_identity,
            "git": self._git_state(),
            "course_zones": self.course_zones,
            "controller_config": self.config,
        }
        temporary = self.run_dir / ".run_manifest.json.tmp"
        destination = self.run_dir / "run_manifest.json"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
        temporary.replace(destination)

    def _open_outage_logs(self) -> None:
        schedule_path = self.run_dir / "outage_schedule.json"
        with schedule_path.open("x", encoding="utf-8") as output:
            json.dump(
                self.outage_schedule,
                output,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            output.write("\n")

        self.outage_events_file = (self.run_dir / "outage_events.jsonl").open(
            "x", encoding="utf-8"
        )
        self.display_frames_file = (self.run_dir / "display_frames.csv").open(
            "x", newline="", encoding="utf-8"
        )
        self.display_frames = csv.DictWriter(
            self.display_frames_file,
            fieldnames=[
                "pilot_frame_id",
                "control_step",
                "sim_time_s",
                "host_monotonic_s",
                "display_mode_code",
                "display_mode",
                "event_ordinal",
                "event_id",
                "source_type",
                "source_frame_id",
                "source_sim_time_s",
                "source_age_ms",
                "anchor_sha256",
                "rendered_sha256",
                "hidden_ground_truth_sha256",
                "render_differs_from_ground_truth",
                "schedule_sha256",
            ],
        )
        self.display_frames.writeheader()
        self.display_frames_file.flush()
        self._write_outage_event(
            "run_start",
            mode=self.outage_mode,
            condition_override=self.outage_condition_override,
            event_count=len(self.outage_schedule["events"]),
            seed=self.outage_schedule["seed"],
        )

    def _write_outage_event(self, event_type: str, **details) -> None:
        if self.outage_events_file is None:
            return
        record = {
            "schema_version": 1,
            "type": event_type,
            "control_step": self.step_count,
            "sim_time_s": self.getTime(),
            "host_monotonic_s": time.monotonic(),
            "schedule_sha256": self.outage_schedule["schedule_sha256"],
            **details,
        }
        json.dump(
            record,
            self.outage_events_file,
            sort_keys=True,
            allow_nan=False,
        )
        self.outage_events_file.write("\n")
        self.outage_events_file.flush()

    def _write_input_device_metadata(self) -> None:
        if self.joystick_identity is None:
            return
        metadata = {
            "schema_version": 1,
            "apparatus_id": self.apparatus["apparatus_id"],
            "apparatus_version": self.apparatus["version"],
            "connected_at_utc": datetime.now(timezone.utc).isoformat(),
            "device": self.joystick_identity,
        }
        destination = self.run_dir / "input_device.json"
        if not destination.exists():
            with destination.open("x", encoding="utf-8") as output:
                json.dump(metadata, output, indent=2, sort_keys=True, allow_nan=False)
                output.write("\n")
        event_path = self.run_dir / "input_device_events.jsonl"
        with event_path.open("a", encoding="utf-8") as output:
            json.dump(metadata, output, sort_keys=True, allow_nan=False)
            output.write("\n")

    def _open_telemetry_segment(self) -> None:
        suffix = "" if self.telemetry_segment_index == 0 else f"_{self.telemetry_segment_index:03d}"
        path = self.run_dir / f"telemetry{suffix}.csv"
        self.telemetry_file = path.open("x", newline="", encoding="utf-8")
        self.telemetry = csv.DictWriter(
            self.telemetry_file,
            fieldnames=self._telemetry_fieldnames(),
        )
        self.telemetry.writeheader()
        self.telemetry_file.flush()

    def _rotate_telemetry_if_needed(self) -> None:
        if self.telemetry_file is None:
            return
        if self.telemetry_file.tell() < self.telemetry_segment_bytes:
            return
        self.telemetry_file.flush()
        self.telemetry_file.close()
        self.telemetry_segment_index += 1
        if self.telemetry_segment_index >= self.telemetry_max_segments:
            self.telemetry_file = None
            self.telemetry = None
            print(
                "Telemetry size limit reached; flight continues without further CSV logging."
            )
            return
        self._open_telemetry_segment()

    def _enable_device(self, name: str, sampling_period: int | None = None):
        device = self.getDevice(name)
        if device is None:
            raise RuntimeError(f'Required Webots device "{name}" is missing')
        device.enable(self.time_step if sampling_period is None else sampling_period)
        return device

    @classmethod
    def _telemetry_fieldnames(cls) -> list[str]:
        fields = [
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
            "vx_mps",
            "vy_mps",
            "vz_mps",
            "wx_radps",
            "wy_radps",
            "wz_radps",
            "gyro_x_radps",
            "gyro_y_radps",
            "gyro_z_radps",
            "ax_mps2",
            "ay_mps2",
            "az_mps2",
            "command_roll",
            "command_pitch",
            "command_yaw",
            "command_throttle",
            "commanded_thrust",
            "target_roll_rate_deg_s",
            "target_pitch_rate_deg_s",
            "target_yaw_rate_deg_s",
            "armed",
            "motor_front_left_rad_s",
            "motor_front_right_rad_s",
            "motor_rear_left_rad_s",
            "motor_rear_right_rad_s",
            "collision",
            "recovery_count",
            "airborne",
            "input_can_arm",
            "rearm_inhibit",
            "rearm_high_seen",
            "boundary_rearm_pending",
            "explicit_arm_required",
            "armed_input_source_code",
            "disarm_event_count",
            "last_disarm_reason_code",
            "input_source_code",
            "hid_report_age_ms",
            "joystick_connected",
            "outage_active",
            "outage_condition_code",
            "outage_event_ordinal",
            "outage_seed",
            "outage_requested_duration_ms",
            "outage_effective_duration_ms",
            "outage_elapsed_ms",
            "outage_anchor_age_ms",
        ]
        fields.extend(f"raw_axis_{index}" for index in range(cls.MAX_RAW_AXES))
        return fields

    def _read_keyboard(self) -> tuple[tuple[float, float, float, float], bool]:
        roll = 0.0
        pitch = 0.0
        yaw = 0.0
        active = False
        key = self.keyboard.getKey()
        while key != -1:
            if key == Keyboard.LEFT:
                roll = 1.0
                active = True
            elif key == Keyboard.RIGHT:
                roll = -1.0
                active = True
            elif key == Keyboard.UP:
                pitch = -1.0
                active = True
            elif key == Keyboard.DOWN:
                pitch = 1.0
                active = True
            elif key in (ord("A"), ord("a")):
                yaw = 1.0
                active = True
            elif key in (ord("D"), ord("d")):
                yaw = -1.0
                active = True
            elif key in (ord("W"), ord("w")):
                self.throttle = clamp(self.throttle + 0.008, 0.0, 1.0)
                active = True
            elif key in (ord("S"), ord("s")):
                self.throttle = clamp(self.throttle - 0.008, 0.0, 1.0)
                active = True
            elif key == ord(" "):
                self.throttle = float(
                    self.flight_profile["keyboard_hover_throttle"]
                )
                active = True
            elif key in (ord("Q"), ord("q")):
                self.manual_disarm_requested = True
                active = True
            elif key in (ord("E"), ord("e")):
                self.manual_arm_requested = True
                active = True
            key = self.keyboard.getKey()
        return (roll, pitch, yaw, self.throttle), active

    def _close_joystick(self, *, retry_next_candidate: bool = False) -> None:
        joystick_path = getattr(self, "joystick_path", None)
        if retry_next_candidate and joystick_path is not None:
            self.last_failed_joystick_path = joystick_path
        if self.joystick is not None:
            try:
                self.joystick.close()
            except (OSError, ValueError):
                pass
        self.joystick = None
        self.joystick_report = []
        self.joystick_report_wall_time = None
        self.joystick_open_wall_time = None
        self.joystick_malformed_since = None
        self.joystick_path = None

    def _read_inputs(self) -> tuple[tuple[float, float, float, float], list[int]]:
        raw_axes = [0] * self.MAX_RAW_AXES
        keyboard_commands, keyboard_active = self._read_keyboard()
        self._refresh_joystick()
        if self.joystick is None:
            self.current_input_source = "keyboard"
            self.input_can_arm = True
            return keyboard_commands, raw_axes

        latest_report = None
        try:
            report = self.joystick.read(64)
            while report:
                latest_report = report
                report = self.joystick.read(64)
        except (OSError, ValueError) as error:
            print(f"Joystick disconnected ({error}); keyboard fallback is active.")
            self._close_joystick()
            self.current_input_source = "invalid"
            self.input_can_arm = False
            return (0.0, 0.0, 0.0, 0.0), raw_axes

        stale_timeout = float(self.config["joystick_stale_timeout_seconds"])
        if latest_report is not None:
            axis_count = min(len(latest_report), self.MAX_RAW_AXES)
            axis_range = int(self.config["axis_range"])
            for index in range(axis_count):
                raw_axes[index] = hid_byte_to_signed(
                    latest_report[index], axis_range
                )
            axes = self.config["axes"]
            configured_axis_indexes = {
                name: int(axes[name])
                for name in ("roll", "pitch", "throttle", "yaw")
            }
            required_axis_count = max(configured_axis_indexes.values()) + 1
            now = time.monotonic()
            if (
                any(index < 0 for index in configured_axis_indexes.values())
                or len(latest_report) < required_axis_count
            ):
                if getattr(self, "joystick_malformed_since", None) is None:
                    self.joystick_malformed_since = now
                unavailable = {
                    name: index
                    for name, index in configured_axis_indexes.items()
                    if index < 0 or index >= len(latest_report)
                }
                details = ", ".join(
                    f"{name}={index}" for name, index in unavailable.items()
                )
                if now - self.joystick_malformed_since > stale_timeout:
                    print(
                        "Apex T19 kept sending malformed reports; closing it and "
                        "trying another interface."
                    )
                    self._close_joystick(retry_next_candidate=True)
                else:
                    print(
                        f"Malformed Apex T19 report ({len(latest_report)} bytes; "
                        f"{details} unavailable); motors disarmed until a complete "
                        "report arrives."
                    )
                self.current_input_source = "invalid"
                self.input_can_arm = False
                return (0.0, 0.0, 0.0, 0.0), raw_axes
            self.joystick_report = latest_report
            self.joystick_report_wall_time = now
            self.joystick_malformed_since = None
            self.last_failed_joystick_path = None
        if not self.joystick_report:
            if (
                self.joystick_open_wall_time is not None
                and time.monotonic() - self.joystick_open_wall_time > stale_timeout
            ):
                print(
                    "Joystick opened but sent no reports; closing it and scanning again."
                )
                self._close_joystick(retry_next_candidate=True)
            self.current_input_source = "invalid"
            self.input_can_arm = False
            return (0.0, 0.0, 0.0, 0.0), raw_axes
        assert self.joystick_report_wall_time is not None
        if time.monotonic() - self.joystick_report_wall_time > stale_timeout:
            print("Joystick reports stopped; motors disarmed until a fresh connection.")
            self._close_joystick(retry_next_candidate=True)
            self.current_input_source = "invalid"
            self.input_can_arm = False
            return (0.0, 0.0, 0.0, 0.0), raw_axes
        axis_count = min(len(self.joystick_report), self.MAX_RAW_AXES)
        axis_range = int(self.config["axis_range"])
        for index in range(axis_count):
            raw_axes[index] = hid_byte_to_signed(
                self.joystick_report[index], axis_range
            )

        axes = self.config["axes"]
        inversions = self.config["invert"]
        deadzone = float(self.config["deadzone"])
        configured_axis_indexes = {
            name: int(axes[name]) for name in ("roll", "pitch", "throttle", "yaw")
        }
        def mapped(name: str) -> float:
            index = configured_axis_indexes[name]
            return normalize_axis(
                raw_axes[index],
                axis_range,
                invert=bool(inversions[name]),
                deadzone=deadzone,
            )

        throttle_index = configured_axis_indexes["throttle"]
        throttle = normalize_throttle(
            raw_axes[throttle_index],
            axis_range,
            invert=bool(inversions["throttle"]),
        )
        joystick_commands = (
            mapped("roll"),
            mapped("pitch"),
            mapped("yaw"),
            throttle,
        )
        # When the Apex is connected, ignore keyboard flight commands so an
        # accidental arrow/WASD press cannot replace the radio throttle.
        del keyboard_active
        self.current_input_source = "joystick"
        self.input_can_arm = True
        return joystick_commands, raw_axes

    def _refresh_joystick(self) -> None:
        if not self.joystick_enabled:
            return
        sim_time = self.getTime()
        if self.joystick is not None or sim_time - self.last_joystick_scan_time < 1.0:
            return
        self.last_joystick_scan_time = sim_time
        candidates = []
        try:
            devices = hid.enumerate()
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Could not scan for Apex T19 ({error}); keyboard fallback is active.")
            return
        for device in devices:
            manufacturer = str(device.get("manufacturer_string") or "")
            product = str(device.get("product_string") or "")
            name = f"{manufacturer} {product}".strip()
            if "APEX T19" not in name.upper():
                continue
            if device.get("usage_page") == 1 and device.get("usage") == 5:
                candidates.append((device, name))
        if not candidates:
            return
        candidates.sort(
            key=lambda candidate: candidate[0].get("path")
            == self.last_failed_joystick_path
        )
        for device_info, joystick_name in candidates:
            joystick = None
            try:
                joystick = hid.device()
                joystick.open_path(device_info["path"])
                joystick.set_nonblocking(True)
                self.joystick = joystick
                self.joystick_name = joystick_name
                self.joystick_path = device_info["path"]
                self.joystick_identity = {
                    "manufacturer": str(device_info.get("manufacturer_string") or ""),
                    "product": str(device_info.get("product_string") or ""),
                    "vendor_id": int(device_info.get("vendor_id") or 0),
                    "product_id": int(device_info.get("product_id") or 0),
                    "usage_page": int(device_info.get("usage_page") or 0),
                    "usage": int(device_info.get("usage") or 0),
                    "interface_number": int(device_info.get("interface_number") or 0),
                }
                self.joystick_report = []
                self.joystick_report_wall_time = None
                self.joystick_open_wall_time = time.monotonic()
                self.joystick_malformed_since = None
                self._write_input_device_metadata()
                print(f'Joystick connected through HIDAPI: "{self.joystick_name}"')
                return
            except (OSError, ValueError, RuntimeError) as error:
                print(f"Could not open Apex T19 ({error}); trying another interface.")
                if joystick is not None:
                    try:
                        joystick.close()
                    except (OSError, ValueError):
                        pass

    def _stop_motors(self, *, best_effort: bool = False) -> None:
        first_error = None
        for motor in self.motors.values():
            try:
                motor.setVelocity(0.0)
            except (OSError, RuntimeError, ValueError) as error:
                if first_error is None:
                    first_error = error
        if first_error is not None and not best_effort:
            raise first_error

    def _disarm(
        self,
        reason: str,
        *,
        reason_code: int,
        required_source: str | None = None,
        explicit_arm: bool = False,
    ) -> None:
        if reason_code != 4:
            # A later emergency stop or input fault must never inherit the
            # easier boundary-recovery gesture.
            self.boundary_rearm_pending = False
            self.boundary_neutral_since = None
        self._stop_motors()
        self.armed = False
        self.airborne = False
        self.ground_contact_since = None
        self.rate_integral = [0.0, 0.0, 0.0]
        self.last_motor_speeds = (0.0, 0.0, 0.0, 0.0)
        self.last_commanded_thrust = 0.0
        self.last_rate_setpoints_deg_s = (0.0, 0.0, 0.0)
        self.last_gyro_values = (0.0, 0.0, 0.0)
        self.armed_input_source = None
        previous_source_required = self.rearm_source_required
        previous_explicit_arm_required = self.explicit_arm_required
        self.rearm_inhibit = True
        self.rearm_source_required = (
            previous_source_required
            if previous_source_required is not None
            else required_source
        )
        self.rearm_high_seen = False
        self.explicit_arm_required = previous_explicit_arm_required or explicit_arm
        # An E press is valid only in the same control step in which the
        # explicit low-throttle rearm check consumes it. Never carry an old
        # request across a disarm or into a later low-throttle state.
        self.manual_arm_requested = False
        self.disarm_event_count += 1
        self.last_disarm_reason_code = reason_code
        if reason:
            print(reason)

    def _set_motor_inputs(
        self,
        roll: float,
        pitch: float,
        yaw: float,
        throttle: float,
        gyro_values: list[float],
    ) -> None:
        arm_requested_this_step = self.manual_arm_requested
        self.manual_arm_requested = False
        rates = self.flight_profile["rates"]
        def rate_setpoint(axis: str, stick: float) -> float:
            settings = rates[axis]
            return betaflight_rate_setpoint(
                stick,
                float(settings["rc_rate"]),
                float(settings["super_rate"]),
                float(settings["expo"]),
            )

        roll_rate = rate_setpoint("roll", roll)
        pitch_rate = rate_setpoint("pitch", pitch)
        yaw_rate = rate_setpoint("yaw", yaw)

        # These are true body-rate error controllers, so centering the stick
        # stops rotation without commanding a level attitude. The pitch motor
        # allocation has the opposite polarity from roll and yaw.
        gains = self.flight_profile["rate_gain"]
        integral_gains = self.flight_profile["rate_integral_gain"]
        integral_limit = float(self.flight_profile["rate_integral_limit"])
        maximum_corrections = self.flight_profile["max_axis_thrust_correction"]
        correction_limits = [
            float(maximum_corrections[axis])
            for axis in ("roll", "pitch", "yaw")
        ]
        ground_control_height = float(
            self.flight_profile["ground_control_height_m"]
        )
        arm_throttle_max = float(self.flight_profile["arm_throttle_max"])
        position = self.drone_node.getPosition()
        grounded = bool(self.drone_node.getContactPoints(True))
        velocity = self.drone_node.getVelocity()
        linear_speed = math.sqrt(sum(value * value for value in velocity[:3]))
        angular_speed = math.sqrt(sum(value * value for value in velocity[3:]))
        if position[2] > ground_control_height and not grounded:
            self.airborne = True
        supported_and_still = (
            grounded
            and position[2] <= ground_control_height
            and linear_speed <= float(self.flight_profile["landed_linear_speed_m_s"])
            and angular_speed <= float(self.flight_profile["landed_angular_speed_rad_s"])
        )
        if self.airborne and supported_and_still and throttle <= arm_throttle_max:
            if self.ground_contact_since is None:
                self.ground_contact_since = self.getTime()
            elif (
                self.getTime() - self.ground_contact_since
                >= float(self.flight_profile["landed_detection_seconds"])
            ):
                self.airborne = False
        else:
            self.ground_contact_since = None
        if not self.airborne and throttle <= arm_throttle_max:
            correction_limits = [0.0, 0.0, 0.0]
        elif throttle <= arm_throttle_max:
            airmode_corrections = self.flight_profile[
                "airmode_axis_thrust_correction"
            ]
            correction_limits = [
                min(limit, float(airmode_corrections[axis]))
                for limit, axis in zip(
                    correction_limits,
                    ("roll", "pitch", "yaw"),
                )
            ]
        setpoints_rad_s = (
            radians(-roll_rate),
            radians(-pitch_rate),
            radians(yaw_rate),
        )
        errors = (
            setpoints_rad_s[0] - gyro_values[0],
            gyro_values[1] - setpoints_rad_s[1],
            setpoints_rad_s[2] - gyro_values[2],
        )
        sticks = (roll, pitch, yaw)
        seconds_per_step = self.time_step / 1000.0
        if throttle <= arm_throttle_max:
            self.rate_integral = [0.0, 0.0, 0.0]
        else:
            for index, axis in enumerate(("roll", "pitch", "yaw")):
                # Center-stick integration removes steady drift. At large stick
                # deflections it is relaxed to avoid windup during flips/rolls.
                if abs(sticks[index]) < 0.5:
                    self.rate_integral[index] = clamp(
                        self.rate_integral[index]
                        + integral_gains[axis] * errors[index] * seconds_per_step,
                        -integral_limit,
                        integral_limit,
                    )
        roll_input = clamp(
            gains["roll"] * errors[0] + self.rate_integral[0],
            -correction_limits[0],
            correction_limits[0],
        )
        pitch_input = clamp(
            gains["pitch"] * errors[1] + self.rate_integral[1],
            -correction_limits[1],
            correction_limits[1],
        )
        yaw_input = clamp(
            gains["yaw"] * errors[2] + self.rate_integral[2],
            -correction_limits[2],
            correction_limits[2],
        )
        self.last_rate_setpoints_deg_s = (-roll_rate, -pitch_rate, yaw_rate)

        if not self.armed:
            if self.rearm_inhibit:
                correct_source = (
                    self.rearm_source_required is None
                    or self.current_input_source == self.rearm_source_required
                )
                if (
                    self.input_can_arm
                    and correct_source
                    and throttle
                    >= float(self.flight_profile["rearm_reset_high_throttle"])
                ):
                    self.rearm_high_seen = True
                if self.explicit_arm_required:
                    rearm_ready = arm_requested_this_step
                elif self.boundary_rearm_pending:
                    # A boundary teleport is already a clear reset event. Arm
                    # only after a sustained, fully neutral control sample so
                    # an old maneuver cannot launch the aircraft sideways.
                    controls_neutral = (
                        throttle <= arm_throttle_max
                        and max(abs(roll), abs(pitch), abs(yaw))
                        <= float(self.flight_profile["boundary_rearm_stick_max"])
                    )
                    if controls_neutral:
                        if self.boundary_neutral_since is None:
                            self.boundary_neutral_since = self.getTime()
                        rearm_ready = (
                            self.getTime() - self.boundary_neutral_since
                            >= float(
                                self.flight_profile[
                                    "boundary_rearm_neutral_seconds"
                                ]
                            )
                        )
                    else:
                        self.boundary_neutral_since = None
                        rearm_ready = False
                else:
                    rearm_ready = self.rearm_high_seen
                if (
                    self.input_can_arm
                    and throttle <= arm_throttle_max
                    and correct_source
                    and rearm_ready
                ):
                    self.rearm_inhibit = False
                    self.rearm_source_required = None
                    self.rearm_high_seen = False
                    self.boundary_rearm_pending = False
                    self.boundary_neutral_since = None
                    self.explicit_arm_required = False
                self.last_motor_speeds = (0.0, 0.0, 0.0, 0.0)
                self.last_commanded_thrust = 0.0
                self._stop_motors()
                return
            if (
                self.input_can_arm
                and throttle <= arm_throttle_max
            ):
                self.armed = True
                self.armed_input_source = self.current_input_source
                print("Simulated motors armed: FPV rate profile active.")
            else:
                self.last_motor_speeds = (0.0, 0.0, 0.0, 0.0)
                self.last_commanded_thrust = 0.0
                self._stop_motors()
                return

        idle_speed = float(self.flight_profile["motor_idle_speed"])
        max_speed = float(self.flight_profile["motor_max_speed"])
        commanded_thrust = shape_throttle(
            throttle,
            float(self.flight_profile["throttle_exponent"]),
        )
        self.last_commanded_thrust = commanded_thrust
        corrections = (
            -yaw_input + pitch_input + roll_input,
            yaw_input + pitch_input - roll_input,
            yaw_input - pitch_input + roll_input,
            -yaw_input - pitch_input - roll_input,
        )
        front_left, front_right, rear_left, rear_right = thrust_mix_to_motor_speeds(
            commanded_thrust,
            corrections,
            idle_speed,
            max_speed,
        )
        self.last_motor_speeds = (
            front_left,
            front_right,
            rear_left,
            rear_right,
        )
        self.motors["front_left"].setVelocity(
            front_left
        )
        self.motors["front_right"].setVelocity(
            -front_right
        )
        self.motors["rear_left"].setVelocity(
            -rear_left
        )
        self.motors["rear_right"].setVelocity(
            rear_right
        )

    def _apply_test_rate_impulse(self) -> None:
        """Inject a deterministic airborne disturbance for physics regression tests."""
        if not self.rate_impulse_step or self.step_count != self.rate_impulse_step:
            return
        translation = list(self.drone_node.getField("translation").getSFVec3f())
        translation[2] = float(os.environ.get("DREAM_MODE_TEST_ALTITUDE", "2.0"))
        self.drone_node.getField("translation").setSFVec3f(translation)
        self.drone_node.getField("rotation").setSFRotation([0.0, 0.0, 1.0, 0.0])
        self.drone_node.setVelocity([0.0, 0.0, 0.0, *self.initial_rate_impulse])
        self.rate_integral = [0.0, 0.0, 0.0]

    def _apply_test_inverted_crash(self) -> None:
        """Place the quad upside down for deterministic recovery testing."""
        if (
            not self.test_inverted_crash_step
            or self.step_count != self.test_inverted_crash_step
        ):
            return
        translation = list(self.home_translation)
        translation[2] = 0.025
        self.drone_node.getField("translation").setSFVec3f(translation)
        self.drone_node.getField("rotation").setSFRotation(
            [1.0, 0.0, 0.0, math.pi]
        )
        self.drone_node.setVelocity([0.0] * 6)
        self.rate_integral = [0.0, 0.0, 0.0]

    def _reset_main_viewpoint(self) -> None:
        """Re-seat the mounted FPV view after a supervisor teleport."""
        if (
            self.viewpoint_node is None
            or self.home_viewpoint_position is None
            or self.home_viewpoint_orientation is None
        ):
            return
        follow_field = self.viewpoint_node.getField("follow")
        # Breaking and restoring the follow link forces Webots to discard the
        # pre-teleport mounted-camera transform.
        follow_field.setSFString("")
        self.viewpoint_node.getField("position").setSFVec3f(
            self.home_viewpoint_position
        )
        self.viewpoint_node.getField("orientation").setSFRotation(
            self.home_viewpoint_orientation
        )
        if self.home_viewpoint_follow_type is not None:
            self.viewpoint_node.getField("followType").setSFString(
                self.home_viewpoint_follow_type
            )
        if self.home_viewpoint_follow_smoothness is not None:
            self.viewpoint_node.getField("followSmoothness").setSFFloat(
                self.home_viewpoint_follow_smoothness
            )
        if self.home_viewpoint_field_of_view is not None:
            self.viewpoint_node.getField("fieldOfView").setSFFloat(
                self.home_viewpoint_field_of_view
            )
        follow_field.setSFString(self.home_viewpoint_follow or "Dream Mode Drone")

    def _return_home(self, message: str, *, reason_code: int) -> None:
        self._abort_active_outage("vehicle_recovery")
        required_source = self.armed_input_source
        preserve_stronger_gesture = (
            self.rearm_inhibit and not self.boundary_rearm_pending
        )
        self.drone_node.getField("translation").setSFVec3f(self.home_translation)
        self.drone_node.getField("rotation").setSFRotation(self.home_rotation)
        self.drone_node.resetPhysics()
        self._reset_main_viewpoint()
        self._disarm(
            "",
            reason_code=reason_code,
            required_source=required_source,
        )
        if not preserve_stronger_gesture:
            self.boundary_rearm_pending = True
        self.boundary_neutral_since = None
        self.inverted_since = None
        self.recovery_count += 1
        print(message)

    def _recover_if_invalid_physics(self) -> bool:
        if not self.recovery_enabled:
            return False
        values = (
            list(self.drone_node.getPosition())
            + list(self.drone_node.getVelocity())
            + list(self.drone_node.getOrientation())
        )
        if all(math.isfinite(value) for value in values):
            return False
        self._return_home(
            "Invalid physics state detected: drone returned to the start. "
            "Center all three axes and lower throttle fully to arm again.",
            reason_code=6,
        )
        return True

    def _recover_if_out_of_bounds(self) -> bool:
        if not self.recovery_enabled:
            return False
        position = self.drone_node.getPosition()
        bounds = self.flight_profile["recovery_bounds"]
        outside = any(
            value < float(limits[0]) or value > float(limits[1])
            for value, limits in zip(
                position,
                (bounds["x_m"], bounds["y_m"], bounds["z_m"]),
            )
        )
        if not outside:
            return False
        self._return_home(
            "Course boundary reached: drone returned to the start. "
            "Center all three axes and lower throttle fully to arm again.",
            reason_code=4,
        )
        return True

    def _recover_if_stuck_inverted(self) -> bool:
        if not self.recovery_enabled:
            return False
        orientation = self.drone_node.getOrientation()
        velocity = self.drone_node.getVelocity()
        grounded = bool(self.drone_node.getContactPoints(True))
        linear_speed = math.sqrt(sum(value * value for value in velocity[:3]))
        angular_speed = math.sqrt(sum(value * value for value in velocity[3:]))
        tilt_limit = math.cos(
            math.radians(float(self.flight_profile["crash_recovery_tilt_deg"]))
        )
        stuck_inverted = (
            grounded
            and orientation[8] <= tilt_limit
            and linear_speed
            <= float(self.flight_profile["landed_linear_speed_m_s"])
            and angular_speed
            <= float(self.flight_profile["landed_angular_speed_rad_s"])
        )
        if not stuck_inverted:
            self.inverted_since = None
            return False
        if self.inverted_since is None:
            self.inverted_since = self.getTime()
            return False
        if (
            self.getTime() - self.inverted_since
            < float(self.flight_profile["crash_recovery_seconds"])
        ):
            return False
        self._return_home(
            "Inverted crash detected: drone returned upright to the start. "
            "Center all three axes and lower throttle fully to arm again.",
            reason_code=5,
        )
        return True

    def _capture_initial_frames(self) -> None:
        if self.captured_initial_frames:
            return
        if not self.capture_initial_frames_enabled:
            if not self.continuous_research_sensors:
                self.depth.disable()
            self.captured_initial_frames = True
            return
        sim_time = self.getTime()
        first_synchronized_frame_time = (
            max(self.CAMERA_PERIOD_MS, self.DEPTH_PERIOD_MS) / 1000.0
        )
        if sim_time + 1e-9 < first_synchronized_frame_time:
            return
        self.camera.saveImage(str(self.run_dir / "rgb_initial.png"), 100)
        self.depth.saveImage(str(self.run_dir / "depth_initial.png"), 100)

        depth_values = array("f", self.depth.getRangeImage())
        with (self.run_dir / "depth_initial.f32").open("xb") as depth_file:
            depth_values.tofile(depth_file)
        metadata = {
            "sim_time_s": sim_time,
            "apparatus_id": self.apparatus["apparatus_id"],
            "apparatus_version": self.apparatus["version"],
            "apparatus_manifest_sha256": self.apparatus_manifest_sha256,
            "controller_config_sha256": self.controller_config_sha256,
            "run_fingerprint_sha256": self.run_fingerprint_sha256,
            "random_seed": self.run_seed,
            "width": self.depth.getWidth(),
            "height": self.depth.getHeight(),
            "field_of_view_rad": self.depth.getFov(),
            "min_range_m": self.depth.getMinRange(),
            "max_range_m": self.depth.getMaxRange(),
            "units": "metres",
            "encoding": "native-endian float32, row-major",
        }
        with (self.run_dir / "depth_initial.json").open("x", encoding="utf-8") as file:
            json.dump(metadata, file, indent=2)
            file.write("\n")
        self.captured_initial_frames = True
        if not self.continuous_research_sensors:
            self.depth.disable()

    def _clear_pilot_display_layer(self) -> None:
        self.pilot_display.setColor(0x000000)
        self.pilot_display.setAlpha(0.0)
        self.pilot_display.setOpacity(1.0)
        self.pilot_display.fillRectangle(
            0,
            0,
            self.pilot_display.getWidth(),
            self.pilot_display.getHeight(),
        )

    def _restore_live_pilot_display(self) -> None:
        self._clear_pilot_display_layer()
        self.pilot_display.attachCamera(self.pilot_camera)
        if self.outage_display_image is not None:
            self.pilot_display.imageDelete(self.outage_display_image)
            self.outage_display_image = None

    def _capture_depth_snapshot(self) -> tuple[array, dict]:
        values = array("f", self.depth.getRangeImage())
        metadata = {
            "schema_version": 1,
            "sim_time_s": self.getTime(),
            "control_step": self.step_count,
            "width": self.depth.getWidth(),
            "height": self.depth.getHeight(),
            "field_of_view_rad": self.depth.getFov(),
            "min_range_m": self.depth.getMinRange(),
            "max_range_m": self.depth.getMaxRange(),
            "units": "metres",
            "encoding": "native-endian float32, row-major",
            "run_fingerprint_sha256": self.run_fingerprint_sha256,
            "schedule_sha256": self.outage_schedule["schedule_sha256"],
        }
        return values, metadata

    def _outage_flight_state(self) -> dict:
        position = list(self.drone_node.getPosition())
        velocity = list(self.drone_node.getVelocity())
        zone_code, zone_id = classify_course_zone(position, self.course_zones)
        return {
            "course_zone_code": zone_code,
            "course_zone_id": zone_id,
            "position_m": position,
            "velocity_m_s_rad_s": velocity,
            "recovery_count": self.recovery_count,
            "armed": self.armed,
            "airborne": self.airborne,
        }

    def _begin_outage(
        self,
        event: dict,
        hidden_frame: bytes | None,
        pilot_frame: bytes | None,
        pilot_frame_sha256: str | None,
    ) -> None:
        processing_started = time.monotonic()
        if (
            hidden_frame is None
            or pilot_frame is None
            or pilot_frame_sha256 is None
        ):
            raise RuntimeError(
                f"Outage {event['id']} did not start on a valid RGB-D anchor frame"
            )
        directory = self.run_dir / "outages" / (
            f"{int(event['ordinal']):03d}_{event['id']}"
        )
        directory.mkdir(parents=True, exist_ok=False)
        self.outage_artifact_dir = directory
        self.outage_midpoint_saved = False
        self.outage_anchor_frame_id = self.last_pilot_live_frame_id
        self.outage_anchor_frame_time = self.last_pilot_live_frame_time
        self.outage_anchor_sha256 = pilot_frame_sha256
        self.pilot_frame_selector.begin(str(event["condition"]), pilot_frame)

        depth_values, depth_metadata = self._capture_depth_snapshot()
        self.outage_display_image = self.pilot_display.imageCopy(
            0,
            0,
            self.pilot_display.getWidth(),
            self.pilot_display.getHeight(),
        )
        if not self.outage_display_image:
            raise RuntimeError(
                f"Could not capture pilot display anchor for outage {event['id']}"
            )
        validation_capture_processing_ms = 0.0
        if self.display_readback_validation_enabled:
            validation_started = time.monotonic()
            self.pilot_display.imageSave(
                self.outage_display_image,
                str(directory / "pilot_anchor.png"),
            )
            validation_capture_processing_ms += (
                time.monotonic() - validation_started
            ) * 1000.0
        self.outage_current_artifact = {
            "directory": directory,
            "event_id": str(event["id"]),
            "anchor_rgb": bytes(hidden_frame),
            "anchor_depth_values": depth_values,
            "anchor_depth_metadata": depth_metadata,
            "hidden_mid_rgb": None,
            "return_rgb": None,
            "pilot_frames": {
                "pilot_anchor.png": bytes(pilot_frame),
                "pilot_source_anchor.png": bytes(pilot_frame),
            },
        }
        self.outage_artifact_records.append(self.outage_current_artifact)
        self.pilot_display.detachCamera()

        effective_condition = str(event["condition"])
        if effective_condition == "frozen":
            self.pilot_display.imagePaste(
                self.outage_display_image,
                0,
                0,
                False,
            )
        else:
            self.pilot_display.setColor(0x000000)
            self.pilot_display.setAlpha(1.0)
            self.pilot_display.setOpacity(1.0)
            self.pilot_display.fillRectangle(
                0,
                0,
                self.pilot_display.getWidth(),
                self.pilot_display.getHeight(),
            )

        command_processing_ms = (
            (time.monotonic() - processing_started) * 1000.0
            - validation_capture_processing_ms
        )
        if self.viewpoint_capture_validation_enabled:
            self.exportImage(str(directory / "viewpoint_anchor.png"), 100)

        self.last_outage_transition = {
            "type": "start",
            "event_id": event["id"],
            "control_step": self.step_count,
        }
        total_processing_ms = (time.monotonic() - processing_started) * 1000.0
        self._write_outage_event(
            "start",
            event=event,
            effective_condition=effective_condition,
            command_step=self.step_count,
            visible_from_step=self.step_count + 1,
            visible_from_sim_time_s=(self.step_count + 1) * self.time_step / 1000.0,
            anchor_frame_id=self.outage_anchor_frame_id,
            anchor_sim_time_s=self.outage_anchor_frame_time,
            anchor_sha256=self.outage_anchor_sha256,
            controller_processing_ms=command_processing_ms,
            validation_capture_processing_ms=(
                total_processing_ms
                - command_processing_ms
            ),
            artifact_directory=str(directory.relative_to(self.run_dir)),
            **self._outage_flight_state(),
        )

    def _finish_outage(self, event: dict, *, abort_reason: str | None = None) -> None:
        record = self.outage_current_artifact
        if record is not None:
            hidden_frame = bytes(self.camera.getImage())
            pilot_frame = bytes(self.pilot_camera.getImage())
            pilot_last, _ = self.pilot_frame_selector.select(
                str(event["condition"]), pilot_frame
            )
            record["pilot_frames"]["pilot_last.png"] = pilot_last
            if self.display_readback_validation_enabled:
                self.pilot_display.imageSave(
                    None,
                    str(record["directory"] / "pilot_last.png"),
                )
            record["return_rgb"] = hidden_frame
            self.outage_pending_return_captures.append(
                (self.step_count + self.CAMERA_PERIOD_MS // self.time_step, record)
            )
        self._restore_live_pilot_display()
        transition_type = "abort" if abort_reason is not None else "end"
        actual_duration_ms = (
            self.step_count - int(event["start_step"])
        ) * self.time_step
        self.last_outage_transition = {
            "type": transition_type,
            "event_id": event["id"],
            "control_step": self.step_count,
        }
        self._write_outage_event(
            transition_type,
            event=event,
            abort_reason=abort_reason,
            command_step=self.step_count,
            live_visible_from_step=self.step_count + 1,
            live_visible_from_sim_time_s=(self.step_count + 1)
            * self.time_step
            / 1000.0,
            actual_duration_ms=actual_duration_ms,
            **self._outage_flight_state(),
        )
        self.pilot_frame_selector.end()
        self.outage_anchor_frame_id = None
        self.outage_anchor_frame_time = None
        self.outage_anchor_sha256 = None
        self.outage_artifact_dir = None
        self.outage_current_artifact = None
        self.outage_midpoint_saved = False

    def _abort_active_outage(self, reason: str) -> None:
        if not hasattr(self, "outage_runtime"):
            return
        transition = self.outage_runtime.abort(self.step_count, reason)
        if transition is not None:
            self._finish_outage(transition["event"], abort_reason=reason)

    def _save_outage_midpoint(self) -> None:
        if self.outage_current_artifact is None or self.outage_midpoint_saved:
            return
        self.outage_current_artifact["hidden_mid_rgb"] = bytes(
            self.camera.getImage()
        )
        pilot_frame = bytes(self.pilot_camera.getImage())
        pilot_mid, _ = self.pilot_frame_selector.select(
            self.outage_runtime.mode,
            pilot_frame,
        )
        self.outage_current_artifact["pilot_frames"]["pilot_mid.png"] = pilot_mid
        if self.display_readback_validation_enabled:
            self.pilot_display.imageSave(
                None,
                str(self.outage_current_artifact["directory"] / "pilot_mid.png"),
            )
        if self.viewpoint_capture_validation_enabled:
            self.exportImage(
                str(self.outage_current_artifact["directory"] / "viewpoint_mid.png"),
                100,
            )
        self.outage_midpoint_saved = True

    def _capture_due_pilot_returns(self) -> None:
        remaining = []
        for due_step, record in self.outage_pending_return_captures:
            if self.step_count < due_step:
                remaining.append((due_step, record))
                continue
            pilot_frame = bytes(self.pilot_camera.getImage())
            record["pilot_frames"]["pilot_return.png"] = pilot_frame
            if self.display_readback_validation_enabled:
                self.pilot_display.imageSave(
                    None,
                    str(record["directory"] / "pilot_return.png"),
                )
            if self.viewpoint_capture_validation_enabled:
                self.exportImage(
                    str(record["directory"] / "viewpoint_return.png"),
                    100,
                )
        self.outage_pending_return_captures = remaining

    def _flush_outage_artifacts(self) -> None:
        if self.outage_artifacts_flushed:
            return
        for record in self.outage_artifact_records:
            directory = record["directory"]
            write_bgra_png(
                directory / "anchor_rgb.png",
                record["anchor_rgb"],
                self.camera.getWidth(),
                self.camera.getHeight(),
            )
            if record["hidden_mid_rgb"] is not None:
                write_bgra_png(
                    directory / "hidden_mid_rgb.png",
                    record["hidden_mid_rgb"],
                    self.camera.getWidth(),
                    self.camera.getHeight(),
                )
            if record["return_rgb"] is not None:
                write_bgra_png(
                    directory / "return_rgb.png",
                    record["return_rgb"],
                    self.camera.getWidth(),
                    self.camera.getHeight(),
                )
            write_depth_bundle(
                directory,
                "anchor_depth",
                record["anchor_depth_values"],
                record["anchor_depth_metadata"],
            )
            for filename, frame in record["pilot_frames"].items():
                path = directory / filename
                if not path.exists():
                    write_bgra_png(
                        path,
                        frame,
                        self.pilot_display.getWidth(),
                        self.pilot_display.getHeight(),
                    )
        self.outage_artifacts_flushed = True

    def _write_display_frame(
        self,
        pilot_frame: bytes,
        hidden_frame_sha256: str,
    ) -> None:
        if self.display_frames is None or self.display_frames_file is None:
            return
        self.pilot_frame_id += 1
        sim_time = self.getTime()
        mode = self.outage_runtime.mode
        current = self.outage_runtime.current
        rendered, source_type = self.pilot_frame_selector.select(mode, pilot_frame)
        rendered_sha256 = hashlib.sha256(rendered).hexdigest()
        if mode == "normal":
            source_frame_id = self.pilot_frame_id
            source_time = sim_time
            source_age_ms = 0.0
            anchor_sha256 = ""
            self.last_pilot_live_frame_id = self.pilot_frame_id
            self.last_pilot_live_frame_time = sim_time
            self.last_pilot_live_frame = bytes(bytearray(pilot_frame))
        elif mode == "frozen" and source_type == "anchor":
            source_frame_id = self.outage_anchor_frame_id
            source_time = self.outage_anchor_frame_time
            source_age_ms = (
                -1.0
                if source_time is None
                else (sim_time - source_time) * 1000.0
            )
            anchor_sha256 = self.outage_anchor_sha256 or ""
        else:
            source_frame_id = ""
            source_time = ""
            source_age_ms = -1.0
            anchor_sha256 = self.outage_anchor_sha256 or ""
        mode_codes = {"normal": 0, "black": 1, "frozen": 2}
        host_time = time.monotonic()
        self.display_frames.writerow(
            {
                "pilot_frame_id": self.pilot_frame_id,
                "control_step": self.step_count,
                "sim_time_s": f"{sim_time:.6f}",
                "host_monotonic_s": f"{host_time:.6f}",
                "display_mode_code": mode_codes[mode],
                "display_mode": mode,
                "event_ordinal": "" if current is None else current["ordinal"],
                "event_id": "" if current is None else current["id"],
                "source_type": source_type,
                "source_frame_id": source_frame_id,
                "source_sim_time_s": source_time,
                "source_age_ms": source_age_ms,
                "anchor_sha256": anchor_sha256,
                "rendered_sha256": rendered_sha256,
                "hidden_ground_truth_sha256": hidden_frame_sha256,
                "render_differs_from_ground_truth": int(
                    rendered_sha256 != hidden_frame_sha256
                ),
                "schedule_sha256": self.outage_schedule["schedule_sha256"],
            }
        )
        if (
            host_time - self.last_display_flush_wall_time
            >= self.TELEMETRY_FLUSH_PERIOD_SECONDS
        ):
            self.display_frames_file.flush()
            self.last_display_flush_wall_time = host_time

    def _update_outage_baseline(self) -> None:
        position = self.drone_node.getPosition()
        zone_code, _ = classify_course_zone(position, self.course_zones)
        on_display_frame = (
            self.step_count
            % (self.CAMERA_PERIOD_MS // self.time_step)
            == 0
        )
        hidden_frame = None
        hidden_frame_sha256 = None
        pilot_frame = None
        pilot_frame_sha256 = None
        if on_display_frame and self.getTime() + 1e-9 >= self.CAMERA_PERIOD_MS / 1000.0:
            hidden_frame = bytes(self.camera.getImage())
            hidden_frame_sha256 = hashlib.sha256(hidden_frame).hexdigest()
            pilot_frame = bytes(self.pilot_camera.getImage())
            pilot_frame_sha256 = hashlib.sha256(pilot_frame).hexdigest()

        # This is the frame that was visible during the Webots step that just
        # completed. Display commands below take effect on the following step,
        # so logging it before transitions aligns evidence with pilot exposure.
        if (
            on_display_frame
            and pilot_frame is not None
            and hidden_frame_sha256 is not None
            and self.display_frame_logging_enabled
        ):
            self._write_display_frame(pilot_frame, hidden_frame_sha256)

        self._capture_due_pilot_returns()

        try:
            transitions = self.outage_runtime.advance(self.step_count, zone_code)
        except OutageRuntimeError as error:
            self._write_outage_event("scheduler_error", message=str(error))
            self._abort_active_outage("scheduler_error")
            self.simulationQuit(1)
            raise RuntimeError(
                f"Phase 2 outage schedule failed: {error}"
            ) from error
        for transition in transitions:
            if transition["type"] == "start":
                self._begin_outage(
                    transition["event"],
                    hidden_frame,
                    pilot_frame,
                    pilot_frame_sha256,
                )
            elif transition["type"] == "end":
                self._finish_outage(transition["event"])

        current = self.outage_runtime.current
        if current is not None and not self.outage_midpoint_saved:
            midpoint_step = int(current["start_step"]) + int(
                current["duration_control_steps"]
            ) // 2
            if self.step_count >= midpoint_step:
                self._save_outage_midpoint()

    def _write_telemetry(
        self,
        commands: tuple[float, float, float, float],
        raw_axes: list[int],
    ) -> None:
        if self.telemetry is None or self.telemetry_file is None:
            return
        sim_time = self.getTime()
        if sim_time < 0.04:
            return
        if sim_time - self.last_log_time + 1e-9 < self.log_period_seconds:
            return

        position = self.drone_node.getPosition()
        velocity = self.drone_node.getVelocity()
        roll, pitch, yaw = self.imu.getRollPitchYaw()
        acceleration = self.accelerometer.getValues()
        collision = bool(self.drone_node.getContactPoints(True))
        host_time = time.monotonic()
        hid_report_age_ms = (
            -1.0
            if self.joystick_report_wall_time is None
            else (host_time - self.joystick_report_wall_time) * 1000.0
        )
        source_codes = {
            "invalid": 0,
            "keyboard": 1,
            "joystick": 2,
            "fixed": 3,
            "scripted": 4,
        }
        input_source_code = source_codes[self.current_input_source]
        armed_input_source_code = source_codes.get(self.armed_input_source, 0)
        course_zone_id, _ = classify_course_zone(position, self.course_zones)
        current_outage = self.outage_runtime.current
        outage_condition_codes = {"normal": 0, "black": 1, "frozen": 2}
        if current_outage is None:
            outage_event_ordinal = 0
            outage_requested_duration_ms = 0.0
            outage_effective_duration_ms = 0.0
            outage_elapsed_ms = 0.0
        else:
            outage_event_ordinal = int(current_outage["ordinal"])
            outage_requested_duration_ms = float(
                current_outage["requested_duration_ms"]
            )
            outage_effective_duration_ms = float(
                current_outage["effective_duration_ms"]
            )
            outage_elapsed_ms = (
                self.step_count - int(current_outage["start_step"])
            ) * self.time_step
        outage_anchor_age_ms = (
            -1.0
            if self.outage_anchor_frame_time is None
            else (sim_time - self.outage_anchor_frame_time) * 1000.0
        )

        row = {
            "control_step": self.step_count,
            "sim_time_s": f"{sim_time:.6f}",
            "host_monotonic_s": f"{host_time:.6f}",
            "run_seed": self.run_seed,
            "apparatus_version_major": self.apparatus_version_parts[0],
            "apparatus_version_minor": self.apparatus_version_parts[1],
            "apparatus_version_patch": self.apparatus_version_parts[2],
            "course_zone_id": course_zone_id,
            "x_m": position[0],
            "y_m": position[1],
            "z_m": position[2],
            "roll_rad": roll,
            "pitch_rad": pitch,
            "yaw_rad": yaw,
            "vx_mps": velocity[0],
            "vy_mps": velocity[1],
            "vz_mps": velocity[2],
            "wx_radps": velocity[3],
            "wy_radps": velocity[4],
            "wz_radps": velocity[5],
            "gyro_x_radps": self.last_gyro_values[0],
            "gyro_y_radps": self.last_gyro_values[1],
            "gyro_z_radps": self.last_gyro_values[2],
            "ax_mps2": acceleration[0],
            "ay_mps2": acceleration[1],
            "az_mps2": acceleration[2],
            "command_roll": commands[0],
            "command_pitch": commands[1],
            "command_yaw": commands[2],
            "command_throttle": commands[3],
            "commanded_thrust": self.last_commanded_thrust,
            "target_roll_rate_deg_s": self.last_rate_setpoints_deg_s[0],
            "target_pitch_rate_deg_s": self.last_rate_setpoints_deg_s[1],
            "target_yaw_rate_deg_s": self.last_rate_setpoints_deg_s[2],
            "armed": int(self.armed),
            "motor_front_left_rad_s": self.last_motor_speeds[0],
            "motor_front_right_rad_s": self.last_motor_speeds[1],
            "motor_rear_left_rad_s": self.last_motor_speeds[2],
            "motor_rear_right_rad_s": self.last_motor_speeds[3],
            "collision": int(collision),
            "recovery_count": self.recovery_count,
            "airborne": int(self.airborne),
            "input_can_arm": int(self.input_can_arm),
            "rearm_inhibit": int(self.rearm_inhibit),
            "rearm_high_seen": int(self.rearm_high_seen),
            "boundary_rearm_pending": int(self.boundary_rearm_pending),
            "explicit_arm_required": int(self.explicit_arm_required),
            "armed_input_source_code": armed_input_source_code,
            "disarm_event_count": self.disarm_event_count,
            "last_disarm_reason_code": self.last_disarm_reason_code,
            "input_source_code": input_source_code,
            "hid_report_age_ms": f"{hid_report_age_ms:.3f}",
            "joystick_connected": int(self.joystick is not None),
            "outage_active": int(current_outage is not None),
            "outage_condition_code": outage_condition_codes[
                self.outage_runtime.mode
            ],
            "outage_event_ordinal": outage_event_ordinal,
            "outage_seed": self.outage_schedule["seed"],
            "outage_requested_duration_ms": outage_requested_duration_ms,
            "outage_effective_duration_ms": outage_effective_duration_ms,
            "outage_elapsed_ms": outage_elapsed_ms,
            "outage_anchor_age_ms": outage_anchor_age_ms,
        }
        row.update(
            {f"raw_axis_{index}": raw_axes[index] for index in range(self.MAX_RAW_AXES)}
        )
        self.telemetry.writerow(row)
        if (
            host_time - self.last_telemetry_flush_wall_time
            >= self.TELEMETRY_FLUSH_PERIOD_SECONDS
        ):
            self.telemetry_file.flush()
            self.last_telemetry_flush_wall_time = host_time
            self._rotate_telemetry_if_needed()
        self.last_log_time = sim_time

    def run(self) -> None:
        run_outcome = "stopped"
        run_error = None
        reload_for_validation = False
        try:
            while self.step(self.time_step) != -1:
                self.step_count += 1
                recovered_this_step = self._recover_if_invalid_physics()
                if not recovered_this_step:
                    recovered_this_step = self._recover_if_out_of_bounds()
                if not recovered_this_step:
                    recovered_this_step = self._recover_if_stuck_inverted()
                commands, raw_axes = self._read_inputs()
                fixed_active = any(value is not None for value in self.fixed_commands)
                commands = tuple(
                    live if fixed is None else fixed
                    for live, fixed in zip(commands, self.fixed_commands)
                )
                if fixed_active:
                    self.current_input_source = "fixed"
                    self.input_can_arm = True
                scripted_commands = self._scripted_commands()
                if scripted_commands is not None:
                    commands = scripted_commands
                    self.current_input_source = "scripted"
                    self.input_can_arm = True
                if (
                    self.test_input_loss_time is not None
                    and not self.test_input_loss_done
                    and self.getTime() >= self.test_input_loss_time
                ):
                    self.current_input_source = "invalid"
                    self.input_can_arm = False
                    self.test_input_loss_done = True
                if (
                    self.test_emergency_stop_time is not None
                    and not self.test_emergency_stop_done
                    and self.getTime() >= self.test_emergency_stop_time
                ):
                    self.manual_disarm_requested = True
                    self.test_emergency_stop_done = True
                if (
                    self.test_explicit_arm_time is not None
                    and not self.test_explicit_arm_done
                    and self.getTime() >= self.test_explicit_arm_time
                ):
                    self.manual_arm_requested = True
                    self.test_explicit_arm_done = True
                if self.manual_disarm_requested:
                    self._disarm(
                        "Emergency motor stop; press E with throttle low to arm again.",
                        reason_code=1,
                        explicit_arm=True,
                    )
                    self.input_can_arm = False
                    self.manual_disarm_requested = False
                elif self.armed and not self.input_can_arm:
                    required_source = self.armed_input_source
                    self._disarm(
                        "Controller input lost; reconnect, move throttle above 10%, then fully down to arm again.",
                        reason_code=2,
                        required_source=required_source,
                    )
                elif (
                    self.armed
                    and self.armed_input_source is not None
                    and self.current_input_source != self.armed_input_source
                ):
                    self._disarm(
                        "Input source changed; move throttle above 10%, then fully down to arm again.",
                        reason_code=3,
                        required_source=self.current_input_source,
                    )
                    self.input_can_arm = False
                elif self.armed and self.armed_input_source is None:
                    self.armed_input_source = self.current_input_source
                if self.rate_impulse_step and self.step_count < self.rate_impulse_step:
                    commands = (0.0, 0.0, 0.0, commands[3])
                self._apply_test_rate_impulse()
                self._apply_test_inverted_crash()
                gyro_values = self.gyro.getValues()
                if not all(math.isfinite(value) for value in gyro_values):
                    if not recovered_this_step:
                        self._return_home(
                            "Invalid gyro state detected: drone returned to the start. "
                            "Center all three axes and lower throttle fully to arm again.",
                            reason_code=6,
                        )
                    gyro_values = [0.0, 0.0, 0.0]
                self.last_gyro_values = tuple(gyro_values)
                self._set_motor_inputs(
                    *commands,
                    gyro_values=gyro_values,
                )
                self._capture_initial_frames()
                self._update_outage_baseline()
                self._write_telemetry(commands, raw_axes)

                if self.smoke_test_steps and self.step_count >= self.smoke_test_steps:
                    self._flush_outage_artifacts()
                    marker = {
                        "steps": self.step_count,
                        "sim_time_s": self.getTime(),
                        "apparatus_id": self.apparatus["apparatus_id"],
                        "apparatus_version": self.apparatus["version"],
                        "controller_config_sha256": self.controller_config_sha256,
                        "run_fingerprint_sha256": self.run_fingerprint_sha256,
                        "rgb_width": self.camera.getWidth(),
                        "rgb_height": self.camera.getHeight(),
                        "depth_width": self.depth.getWidth(),
                        "depth_height": self.depth.getHeight(),
                        "pilot_display_width": self.pilot_display.getWidth(),
                        "pilot_display_height": self.pilot_display.getHeight(),
                        "outage_mode": self.outage_mode,
                        "outage_schedule_sha256": self.outage_schedule[
                            "schedule_sha256"
                        ],
                        "drone_z_m": self.drone_node.getPosition()[2],
                        "yaw_rate_rad_s": self.drone_node.getVelocity()[5],
                        "body_roll_rate_rad_s": self.last_gyro_values[0],
                        "body_pitch_rate_rad_s": self.last_gyro_values[1],
                        "body_yaw_rate_rad_s": self.last_gyro_values[2],
                        "max_motor_speed_rad_s": max(self.last_motor_speeds),
                    }
                    with (self.run_dir / "smoke_test_ok.json").open(
                        "x", encoding="utf-8"
                    ) as marker_file:
                        json.dump(marker, marker_file, indent=2)
                        marker_file.write("\n")
                    if self.telemetry_file is not None:
                        self.telemetry_file.flush()
                    print(f"SMOKE_TEST_OK steps={self.step_count} log={self.run_dir}")
                    run_outcome = "completed"
                    reload_for_validation = self._advance_validation_queue()
                    if not reload_for_validation:
                        self.simulationQuit(0)
                    break
        except Exception as error:
            run_outcome = "error"
            run_error = f"{type(error).__name__}: {error}"
            raise
        finally:
            self._stop_motors(best_effort=True)
            try:
                self._abort_active_outage("controller_exit")
                self._write_outage_event(
                    "run_end",
                    outcome=run_outcome,
                    error=run_error,
                    scheduled_event_count=len(self.outage_runtime.events),
                    started_event_count=len(self.outage_runtime.started_ids),
                    ended_event_count=len(self.outage_runtime.ended_ids),
                    aborted_event_count=len(self.outage_runtime.aborted_ids),
                    pending_event_count=len(self.outage_runtime.pending),
                )
                self._restore_live_pilot_display()
                self._flush_outage_artifacts()
            except (OSError, RuntimeError, ValueError):
                pass
            if self.telemetry_file is not None:
                try:
                    self.telemetry_file.flush()
                    self.telemetry_file.close()
                except (OSError, ValueError):
                    pass
            if self.display_frames_file is not None:
                try:
                    self.display_frames_file.flush()
                    self.display_frames_file.close()
                except (OSError, ValueError):
                    pass
            if self.outage_events_file is not None:
                try:
                    self.outage_events_file.flush()
                    self.outage_events_file.close()
                except (OSError, ValueError):
                    pass
            self._close_joystick()
        if reload_for_validation:
            print("Reloading world for the next single-session validation scenario.")
            self.worldReload()


if __name__ == "__main__":
    DreamModeController(configure_single_session_validation()).run()
