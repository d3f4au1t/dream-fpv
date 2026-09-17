#!/usr/bin/env python3
"""Verify the frozen Dream FPV apparatus without opening Webots."""

from __future__ import annotations

import ast
import json
import math
import platform
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from apparatus import ApparatusIntegrityError, load_and_verify_apparatus
    from outage_baselines import (
        materialize_schedule,
        validate_config as validate_outage_config,
        validate_zone_references,
    )
finally:
    sys.path.remove(str(CONTROLLER_DIR))


class SemanticVerificationError(ValueError):
    """Raised when a manifest claim disagrees with the executable apparatus."""


def require_integer(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticVerificationError(f"{label} must be an integer, not {value!r}")
    return value


def require_close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-8):
        raise SemanticVerificationError(
            f"{label} is {actual!r}, expected {expected!r}"
        )


def number_from(text: str, pattern: str, label: str) -> float:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        raise SemanticVerificationError(f"Could not find {label}")
    return float(match.group(1))


def vector_from(block: str, field: str, count: int) -> list[float]:
    match = re.search(
        rf"^\s*{re.escape(field)}\s+([^\n]+)$",
        block,
        re.MULTILINE,
    )
    if match is None:
        raise SemanticVerificationError(f"Could not find {field}")
    values = [float(value) for value in match.group(1).split()]
    if len(values) != count:
        raise SemanticVerificationError(
            f"{field} contains {len(values)} values, expected {count}"
        )
    return values


def device_block(world: str, device_type: str, name: str) -> str:
    header = re.compile(rf"\b{re.escape(device_type)}\s*\{{")
    name_pattern = re.compile(rf'^\s*name\s+"{re.escape(name)}"\s*$', re.MULTILINE)
    for match in header.finditer(world):
        opening = world.find("{", match.start())
        depth = 0
        in_string = False
        escaped = False
        for index in range(opening, len(world)):
            character = world[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    block = world[match.start() : index + 1]
                    if name_pattern.search(block):
                        return block
                    break
    raise SemanticVerificationError(f'Could not find {device_type} "{name}"')


def controller_constants(controller_source: str) -> dict[str, float]:
    tree = ast.parse(controller_source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DreamModeController":
            constants = {}
            for statement in node.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if target.id in {
                    "LOG_PERIOD_SECONDS",
                    "CAMERA_PERIOD_MS",
                    "DEPTH_PERIOD_MS",
                }:
                    constants[target.id] = float(ast.literal_eval(statement.value))
            return constants
    raise SemanticVerificationError("Could not find DreamModeController constants")


def verify_semantic_claims(apparatus: dict, zones: dict, config: dict) -> None:
    seed = require_integer(apparatus.get("random_seed"), "random_seed")
    simulator = apparatus.get("simulator", {})
    basic_time_step_ms = require_integer(
        simulator.get("basic_time_step_ms"),
        "simulator.basic_time_step_ms",
    )
    timing = apparatus.get("timing", {})
    for name in (
        "rgb_period_ms",
        "depth_period_ms",
        "default_telemetry_period_ms",
    ):
        require_integer(timing.get(name), f"timing.{name}")
    for index, value in enumerate(apparatus["sensors"]["rgb_resolution"]):
        require_integer(value, f"sensors.rgb_resolution[{index}]")
    for index, value in enumerate(apparatus["sensors"]["depth_resolution"]):
        require_integer(value, f"sensors.depth_resolution[{index}]")
    for index, zone in enumerate(zones["zones"]):
        require_integer(zone.get("code"), f"course_zones.zones[{index}].code")
        require_integer(
            zone.get("route_order"),
            f"course_zones.zones[{index}].route_order",
        )

    world = (PROJECT_ROOT / "worlds" / "dream_mode_research.wbt").read_text(
        encoding="utf-8"
    )
    require_close(
        number_from(world, r"^\s*randomSeed\s+([-+0-9.eE]+)", "randomSeed"),
        seed,
        "world randomSeed",
    )
    require_close(
        number_from(
            world,
            r"^\s*basicTimeStep\s+([-+0-9.eE]+)",
            "basicTimeStep",
        ),
        basic_time_step_ms,
        "world basicTimeStep",
    )
    require_close(
        number_from(world, r"^\s*gravity\s+([-+0-9.eE]+)", "gravity"),
        config["flight_profile"]["reference_airframe"]["gravity_m_s2"],
        "world gravity",
    )

    sensors = apparatus["sensors"]
    camera = device_block(world, "Camera", "research camera")
    depth = device_block(world, "RangeFinder", "depth")
    for label, block in (("camera", camera), ("depth", depth)):
        for actual, expected in zip(
            vector_from(block, "translation", 3),
            sensors["camera_translation_m"],
        ):
            require_close(actual, expected, f"{label} translation")
        for actual, expected in zip(
            vector_from(block, "rotation", 4),
            sensors["camera_rotation_axis_angle"],
        ):
            require_close(actual, expected, f"{label} rotation")
        require_close(
            number_from(block, r"^\s*fieldOfView\s+([-+0-9.eE]+)", "fieldOfView"),
            sensors["field_of_view_rad"],
            f"{label} fieldOfView",
        )
    for label, block, resolution in (
        ("camera", camera, sensors["rgb_resolution"]),
        ("depth", depth, sensors["depth_resolution"]),
    ):
        require_close(
            number_from(block, r"^\s*width\s+([-+0-9.eE]+)", "width"),
            resolution[0],
            f"{label} width",
        )
        require_close(
            number_from(block, r"^\s*height\s+([-+0-9.eE]+)", "height"),
            resolution[1],
            f"{label} height",
        )
    require_close(
        number_from(depth, r"^\s*minRange\s+([-+0-9.eE]+)", "minRange"),
        sensors["depth_range_m"][0],
        "depth minRange",
    )
    require_close(
        number_from(depth, r"^\s*maxRange\s+([-+0-9.eE]+)", "maxRange"),
        sensors["depth_range_m"][1],
        "depth maxRange",
    )

    source = (
        PROJECT_ROOT
        / "controllers"
        / "dream_mode_controller"
        / "dream_mode_controller.py"
    ).read_text(encoding="utf-8")
    constants = controller_constants(source)
    expected_constants = {
        "CAMERA_PERIOD_MS": timing["rgb_period_ms"],
        "DEPTH_PERIOD_MS": timing["depth_period_ms"],
        "LOG_PERIOD_SECONDS": timing["default_telemetry_period_ms"] / 1000.0,
    }
    for name, expected in expected_constants.items():
        if name not in constants:
            raise SemanticVerificationError(f"Controller constant {name} is missing")
        require_close(constants[name], expected, f"controller {name}")

    if apparatus.get("version", "").startswith("2."):
        outage_relative = apparatus.get("outage_baseline_config_file")
        if outage_relative != config.get("outage_baseline_config"):
            raise SemanticVerificationError(
                "Outage configuration references disagree"
            )
        outage_config = json.loads(
            (PROJECT_ROOT / outage_relative).read_text(encoding="utf-8")
        )
        validate_outage_config(outage_config)
        deterministic_schedule = materialize_schedule(
            outage_config,
            "deterministic",
        )
        randomized_schedule = materialize_schedule(
            outage_config,
            "randomized",
        )
        validate_zone_references(outage_config, deterministic_schedule, zones)
        validate_zone_references(outage_config, randomized_schedule, zones)

        outage_claim = apparatus.get("video_outages", {})
        if outage_claim.get("conditions") != outage_config.get("conditions"):
            raise SemanticVerificationError("Outage condition claims disagree")
        if outage_claim.get("requested_durations_ms") != outage_config["timing"].get(
            "durations_ms"
        ):
            raise SemanticVerificationError("Requested outage durations disagree")
        effective = [
            int(event["effective_duration_ms"])
            for event in deterministic_schedule["events"][::2]
        ]
        if outage_claim.get("effective_durations_ms") != effective:
            raise SemanticVerificationError("Effective outage durations disagree")
        if outage_claim.get("trigger_alignment_ms") != outage_config["timing"].get(
            "rgbd_anchor_period_ms"
        ):
            raise SemanticVerificationError("Outage trigger alignment disagrees")
        if outage_config["randomized"]["event_count"] >= len(
            outage_config["randomized"]["eligible_triggers"]
        ):
            raise SemanticVerificationError(
                "Randomized schedule must select a proper trigger subset"
            )

        pilot = apparatus.get("pilot_display", {})
        display = device_block(world, "Display", pilot.get("device_name", ""))
        for actual, expected in zip(
            vector_from(display, "translation", 3),
            pilot["translation_m"],
        ):
            require_close(actual, expected, "pilot display translation")
        for actual, expected in zip(
            vector_from(display, "rotation", 4),
            pilot["rotation_axis_angle"],
        ):
            require_close(actual, expected, "pilot display rotation")
        require_close(
            number_from(display, r"^\s*width\s+([-+0-9.eE]+)", "width"),
            pilot["resolution"][0],
            "pilot display width",
        )
        require_close(
            number_from(display, r"^\s*height\s+([-+0-9.eE]+)", "height"),
            pilot["resolution"][1],
            "pilot display height",
        )
        if "geometry IndexedFaceSet" not in display:
            raise SemanticVerificationError("Pilot display surface is missing")
        for token in (
            "appearance PBRAppearance",
            "baseColorMap ImageTexture",
            "roughness 1",
            "metalness 0",
        ):
            if token not in display:
                raise SemanticVerificationError(
                    f"Pilot display texture surface is missing {token!r}"
                )
        if "Group {" in display:
            raise SemanticVerificationError(
                "Pilot display texture Shape must be a direct Display child"
            )
        half_width = float(pilot["surface_size_m"][0]) / 2.0
        half_height = float(pilot["surface_size_m"][1]) / 2.0
        for token in (
            f"0 {half_width:g} {half_height:g}",
            f"0 -{half_width:g} -{half_height:g}",
        ):
            if token not in world:
                raise SemanticVerificationError(
                    "Pilot display surface dimensions disagree"
                )

        viewpoint_match = re.search(
            r"DEF FPV_VIEW Viewpoint\s*\{(?P<body>.*?)\n\}",
            world,
            re.DOTALL,
        )
        if viewpoint_match is None:
            raise SemanticVerificationError("FPV Viewpoint is missing")
        viewpoint = viewpoint_match.group("body")
        clip_near, clip_far = pilot["viewpoint_clip_range_m"]
        require_close(
            number_from(viewpoint, r"^\s*near\s+([-+0-9.eE]+)", "near"),
            clip_near,
            "viewpoint near",
        )
        require_close(
            number_from(viewpoint, r"^\s*far\s+([-+0-9.eE]+)", "far"),
            clip_far,
            "viewpoint far",
        )

        project_view = (
            PROJECT_ROOT / "worlds" / ".dream_mode_research.wbproj"
        ).read_text(encoding="utf-8")
        for device in ("depth", "pilot display", "research camera"):
            expected = (
                "renderingDevicePerspectives: "
                f"Dream Mode Drone:{device};0;"
            )
            if expected not in project_view:
                raise SemanticVerificationError(
                    f"Rendering-device overlay is not hidden: {device}"
                )
        if source.count("setVisibility") < 2:
            raise SemanticVerificationError(
                "Pilot display is not hidden from both research sensors"
            )

    if platform.python_version() != simulator["python_version"]:
        raise SemanticVerificationError(
            "Python version is "
            f"{platform.python_version()}, expected {simulator['python_version']}"
        )
    webots_version_path = Path(
        "/Applications/Webots.app/Contents/Resources/version.txt"
    )
    if not webots_version_path.is_file():
        raise SemanticVerificationError(f"Webots version file is missing: {webots_version_path}")
    installed_webots = webots_version_path.read_text(encoding="utf-8").strip()
    if installed_webots != simulator["tested_version"]:
        raise SemanticVerificationError(
            f"Installed Webots is {installed_webots}, expected {simulator['tested_version']}"
        )


def main() -> int:
    controller_config = json.loads(
        (PROJECT_ROOT / "config" / "controller.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        apparatus, zones, locked_hashes, manifest_hash = load_and_verify_apparatus(
            PROJECT_ROOT,
            controller_config,
        )
        verify_semantic_claims(apparatus, zones, controller_config)
    except (ApparatusIntegrityError, OSError, ValueError) as error:
        print(f"Apparatus verification failed: {error}", file=sys.stderr)
        return 1

    print(
        f"Verified {apparatus['apparatus_id']} v{apparatus['version']} "
        f"({len(locked_hashes)} locked files, {len(zones['zones'])} zones)."
    )
    print(f"Manifest SHA-256: {manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
