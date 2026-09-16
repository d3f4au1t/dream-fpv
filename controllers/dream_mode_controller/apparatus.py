"""Versioned apparatus loading, integrity checks, and course-zone lookup."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Sequence


SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*")


class ApparatusIntegrityError(ValueError):
    """Raised when a frozen apparatus file or definition has drifted."""


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ApparatusIntegrityError(f"{path.name} must contain one JSON object")
    return value


def _project_file(project_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ApparatusIntegrityError(
            f"Apparatus path must stay inside the project: {relative_path}"
        )
    resolved_root = project_root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ApparatusIntegrityError(
            f"Apparatus path escapes the project: {relative_path}"
        )
    return resolved


def _validate_bounds(
    zone: dict,
    recovery_bounds: dict,
) -> None:
    bounds = zone.get("bounds_m")
    if not isinstance(bounds, dict):
        raise ApparatusIntegrityError(f"Zone {zone.get('id')} has no bounds_m")
    for short_axis, recovery_axis in (("x", "x_m"), ("y", "y_m"), ("z", "z_m")):
        interval = bounds.get(short_axis)
        if not isinstance(interval, list) or len(interval) != 2:
            raise ApparatusIntegrityError(
                f"Zone {zone.get('id')} {short_axis} bounds must have two values"
            )
        lower, upper = interval
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in interval):
            raise ApparatusIntegrityError(
                f"Zone {zone.get('id')} {short_axis} bounds must be finite"
            )
        if lower >= upper:
            raise ApparatusIntegrityError(
                f"Zone {zone.get('id')} {short_axis} bounds are not ordered"
            )
        recovery_lower, recovery_upper = recovery_bounds[recovery_axis]
        if lower < recovery_lower or upper > recovery_upper:
            raise ApparatusIntegrityError(
                f"Zone {zone.get('id')} extends beyond recovery bounds"
            )


def _zones_overlap(first: dict, second: dict) -> bool:
    return all(
        max(first["bounds_m"][axis][0], second["bounds_m"][axis][0])
        < min(first["bounds_m"][axis][1], second["bounds_m"][axis][1])
        for axis in ("x", "y", "z")
    )


def validate_course_zones(zones_config: dict, recovery_bounds: dict) -> None:
    if zones_config.get("schema_version") != 1:
        raise ApparatusIntegrityError("Unsupported course-zone schema")
    if zones_config.get("boundary_rule") != "lower-inclusive, upper-exclusive":
        raise ApparatusIntegrityError("Course-zone boundary rule is not locked")

    fallback = zones_config.get("fallback")
    if not isinstance(fallback, dict) or fallback.get("code") != 0:
        raise ApparatusIntegrityError("Course-zone fallback code must be 0")
    if fallback.get("id") != "transfer":
        raise ApparatusIntegrityError("Course-zone fallback must be transfer")

    zones = zones_config.get("zones")
    if not isinstance(zones, list) or not zones:
        raise ApparatusIntegrityError("At least one course zone is required")

    codes: set[int] = set()
    ids: set[str] = set()
    route_orders: set[int] = set()
    for zone in zones:
        if not isinstance(zone, dict):
            raise ApparatusIntegrityError("Every course zone must be an object")
        code = zone.get("code")
        zone_id = zone.get("id")
        route_order = zone.get("route_order")
        if not isinstance(code, int) or code <= 0 or code in codes:
            raise ApparatusIntegrityError(f"Invalid or duplicate zone code: {code}")
        if (
            not isinstance(zone_id, str)
            or SAFE_ID_PATTERN.fullmatch(zone_id) is None
            or zone_id in ids
        ):
            raise ApparatusIntegrityError(f"Invalid or duplicate zone id: {zone_id}")
        if (
            not isinstance(route_order, int)
            or route_order <= 0
            or route_order in route_orders
        ):
            raise ApparatusIntegrityError(
                f"Invalid or duplicate route order: {route_order}"
            )
        if not isinstance(zone.get("research_context"), str):
            raise ApparatusIntegrityError(f"Zone {zone_id} has no research context")
        _validate_bounds(zone, recovery_bounds)
        codes.add(code)
        ids.add(zone_id)
        route_orders.add(route_order)

    for index, first in enumerate(zones):
        for second in zones[index + 1 :]:
            if _zones_overlap(first, second):
                raise ApparatusIntegrityError(
                    f"Course zones overlap: {first['id']} and {second['id']}"
                )


def classify_course_zone(
    position: Sequence[float],
    zones_config: dict,
) -> tuple[int, str]:
    if len(position) < 3 or not all(math.isfinite(float(value)) for value in position[:3]):
        return 0, "transfer"
    for zone in sorted(zones_config["zones"], key=lambda item: item["code"]):
        bounds = zone["bounds_m"]
        if all(
            bounds[axis][0] <= float(position[index]) < bounds[axis][1]
            for index, axis in enumerate(("x", "y", "z"))
        ):
            return int(zone["code"]), str(zone["id"])
    fallback = zones_config["fallback"]
    return int(fallback["code"]), str(fallback["id"])


def load_and_verify_apparatus(
    project_root: Path,
    controller_config: dict,
) -> tuple[dict, dict, dict[str, str], str]:
    manifest_relative = controller_config.get("apparatus_manifest")
    if not isinstance(manifest_relative, str):
        raise ApparatusIntegrityError("controller.json has no apparatus_manifest")
    manifest_path = _project_file(project_root, manifest_relative)
    apparatus = _load_json(manifest_path)

    if apparatus.get("schema_version") != 1:
        raise ApparatusIntegrityError("Unsupported apparatus schema")
    if apparatus.get("status") != "frozen":
        raise ApparatusIntegrityError("The selected apparatus is not frozen")
    if SAFE_ID_PATTERN.fullmatch(str(apparatus.get("apparatus_id", ""))) is None:
        raise ApparatusIntegrityError("Invalid apparatus_id")
    if re.fullmatch(r"\d+\.\d+\.\d+", str(apparatus.get("version", ""))) is None:
        raise ApparatusIntegrityError("Apparatus version must use semantic versioning")

    zones_relative = apparatus.get("course_zones_file")
    if zones_relative != controller_config.get("course_zones_file"):
        raise ApparatusIntegrityError("Course-zone file references disagree")
    if not isinstance(zones_relative, str):
        raise ApparatusIntegrityError("Apparatus has no course_zones_file")
    zones_config = _load_json(_project_file(project_root, zones_relative))
    validate_course_zones(
        zones_config,
        controller_config["flight_profile"]["recovery_bounds"],
    )

    locked_files = apparatus.get("locked_files")
    if not isinstance(locked_files, dict) or not locked_files:
        raise ApparatusIntegrityError("Apparatus has no locked files")
    actual_hashes: dict[str, str] = {}
    mismatches = []
    for relative_path, expected_digest in sorted(locked_files.items()):
        if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
            raise ApparatusIntegrityError("Locked-file entries must be strings")
        if SHA256_PATTERN.fullmatch(expected_digest) is None:
            raise ApparatusIntegrityError(
                f"Invalid SHA-256 for locked file {relative_path}"
            )
        source_path = _project_file(project_root, relative_path)
        if not source_path.is_file():
            raise ApparatusIntegrityError(f"Locked file is missing: {relative_path}")
        actual_digest = file_sha256(source_path)
        actual_hashes[relative_path] = actual_digest
        if actual_digest != expected_digest:
            mismatches.append(relative_path)
    if mismatches:
        raise ApparatusIntegrityError(
            "Frozen apparatus drift detected in: " + ", ".join(mismatches)
        )

    return apparatus, zones_config, actual_hashes, file_sha256(manifest_path)
