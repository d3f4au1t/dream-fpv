"""Deterministic scheduling and frame selection for Phase 2 link outages.

This module deliberately has no Webots dependency.  The simulator controller
uses it at runtime, while unit tests can exercise the timing and display
semantics without launching the simulator.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import random
import re
from typing import Any, Iterable


ALLOWED_CONDITIONS = frozenset(("black", "frozen"))
ALLOWED_MODES = frozenset(("off", "deterministic", "randomized", "scripted"))
SAFE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]*")


class OutageConfigurationError(ValueError):
    """Raised when a Phase 2 configuration or override is invalid."""


class OutageRuntimeError(RuntimeError):
    """Raised when realized outage triggers would overlap or run out of order."""


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutageConfigurationError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise OutageConfigurationError(
            f"{label} must be finite and at least {minimum:g}"
        )
    return result


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OutageConfigurationError(f"{label} must be a positive integer")
    return value


def ceil_to_multiple(value: int, quantum: int) -> int:
    if value < 0 or quantum <= 0:
        raise ValueError("value must be non-negative and quantum must be positive")
    return ((value + quantum - 1) // quantum) * quantum


def _milliseconds_to_steps_ceil(milliseconds: float, control_step_ms: int) -> int:
    return int(math.ceil(milliseconds / control_step_ms - 1e-12))


def quantize_duration(
    requested_duration_ms: float,
    *,
    control_step_ms: int,
    display_period_ms: int,
) -> dict[str, int | float]:
    requested = _finite_number(
        requested_duration_ms,
        "requested_duration_ms",
        minimum=0.000001,
    )
    control_step = _positive_integer(control_step_ms, "control_step_ms")
    display_period = _positive_integer(display_period_ms, "display_period_ms")
    if display_period % control_step != 0:
        raise OutageConfigurationError(
            "display_period_ms must be an integer multiple of control_step_ms"
        )
    display_frames = int(math.ceil(requested / display_period - 1e-12))
    effective_ms = display_frames * display_period
    return {
        "requested_duration_ms": requested_duration_ms,
        "duration_display_frames": display_frames,
        "duration_control_steps": effective_ms // control_step,
        "effective_duration_ms": effective_ms,
        "quantization_error_ms": effective_ms - requested,
    }


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise OutageConfigurationError("Outage configuration must be an object")
    if config.get("schema_version") != 1:
        raise OutageConfigurationError("Unsupported outage configuration schema")
    experiment_id = config.get("experiment_id")
    if not isinstance(experiment_id, str) or SAFE_ID_PATTERN.fullmatch(experiment_id) is None:
        raise OutageConfigurationError("experiment_id must be a safe identifier")
    version = config.get("version")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise OutageConfigurationError("version must use semantic versioning")
    random_seed = config.get("random_seed")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise OutageConfigurationError("random_seed must be an integer")

    timing = config.get("timing")
    if not isinstance(timing, dict):
        raise OutageConfigurationError("timing must be an object")
    control_step_ms = _positive_integer(
        timing.get("control_step_ms"), "timing.control_step_ms"
    )
    display_period_ms = _positive_integer(
        timing.get("display_period_ms"), "timing.display_period_ms"
    )
    anchor_period_ms = _positive_integer(
        timing.get("rgbd_anchor_period_ms"), "timing.rgbd_anchor_period_ms"
    )
    if display_period_ms % control_step_ms != 0:
        raise OutageConfigurationError(
            "display_period_ms must be an integer multiple of control_step_ms"
        )
    if anchor_period_ms % display_period_ms != 0:
        raise OutageConfigurationError(
            "rgbd_anchor_period_ms must be an integer multiple of display_period_ms"
        )
    durations = timing.get("durations_ms")
    if not isinstance(durations, list) or not durations:
        raise OutageConfigurationError("timing.durations_ms must be a non-empty list")
    duration_values = []
    for index, duration in enumerate(durations):
        value = _finite_number(
            duration,
            f"timing.durations_ms[{index}]",
            minimum=0.000001,
        )
        duration_values.append(value)
        quantize_duration(
            value,
            control_step_ms=control_step_ms,
            display_period_ms=display_period_ms,
        )
    if len(set(duration_values)) != len(duration_values):
        raise OutageConfigurationError("timing.durations_ms contains duplicates")

    conditions = config.get("conditions")
    if (
        not isinstance(conditions, list)
        or not conditions
        or any(condition not in ALLOWED_CONDITIONS for condition in conditions)
        or len(set(conditions)) != len(conditions)
    ):
        raise OutageConfigurationError(
            "conditions must be a unique non-empty list containing black/frozen"
        )

    deterministic = config.get("deterministic_events")
    if not isinstance(deterministic, list) or not deterministic:
        raise OutageConfigurationError(
            "deterministic_events must be a non-empty list"
        )
    _validate_event_templates(
        deterministic,
        allowed_durations=set(duration_values),
        allowed_conditions=set(conditions),
    )

    randomized = config.get("randomized")
    if not isinstance(randomized, dict):
        raise OutageConfigurationError("randomized must be an object")
    triggers = randomized.get("eligible_triggers")
    if not isinstance(triggers, list) or not triggers:
        raise OutageConfigurationError(
            "randomized.eligible_triggers must be a non-empty list"
        )
    randomized_trigger_identities: set[tuple[Any, ...]] = set()
    for index, trigger in enumerate(triggers):
        _validate_trigger(trigger, f"randomized.eligible_triggers[{index}]")
        identity = _trigger_identity(trigger)
        if identity in randomized_trigger_identities:
            raise OutageConfigurationError(
                "randomized.eligible_triggers contains duplicate triggers"
            )
        randomized_trigger_identities.add(identity)
    event_count = _positive_integer(
        randomized.get("event_count"), "randomized.event_count"
    )
    if event_count > len(triggers):
        raise OutageConfigurationError(
            "randomized.event_count exceeds the number of eligible triggers"
        )


def _validate_event_templates(
    events: Iterable[dict[str, Any]],
    *,
    allowed_durations: set[float] | None = None,
    allowed_conditions: set[str] | None = None,
) -> None:
    identifiers: set[str] = set()
    trigger_identities: set[tuple[Any, ...]] = set()
    for index, event in enumerate(events):
        label = f"event[{index}]"
        if not isinstance(event, dict):
            raise OutageConfigurationError(f"{label} must be an object")
        event_id = event.get("id")
        if (
            not isinstance(event_id, str)
            or SAFE_ID_PATTERN.fullmatch(event_id) is None
            or event_id in identifiers
        ):
            raise OutageConfigurationError(f"{label}.id is invalid or duplicated")
        identifiers.add(event_id)
        condition = event.get("condition")
        permitted_conditions = allowed_conditions or set(ALLOWED_CONDITIONS)
        if condition not in permitted_conditions:
            raise OutageConfigurationError(f"{label}.condition is invalid")
        duration = _finite_number(
            event.get("duration_ms"),
            f"{label}.duration_ms",
            minimum=0.000001,
        )
        if allowed_durations is not None and duration not in allowed_durations:
            raise OutageConfigurationError(
                f"{label}.duration_ms is not listed in timing.durations_ms"
            )
        trigger = event.get("trigger")
        _validate_trigger(trigger, f"{label}.trigger")
        identity = _trigger_identity(trigger)
        if identity in trigger_identities:
            raise OutageConfigurationError(
                f"{label}.trigger duplicates an earlier trigger"
            )
        trigger_identities.add(identity)


def _validate_trigger(trigger: Any, label: str) -> None:
    if not isinstance(trigger, dict):
        raise OutageConfigurationError(f"{label} must be an object")
    trigger_type = trigger.get("type")
    if trigger_type == "time":
        _finite_number(trigger.get("start_ms"), f"{label}.start_ms")
        unexpected = set(trigger).difference(("type", "start_ms"))
    elif trigger_type == "zone_entry":
        zone_id = trigger.get("zone_id")
        if not isinstance(zone_id, str) or SAFE_ID_PATTERN.fullmatch(zone_id) is None:
            raise OutageConfigurationError(f"{label}.zone_id is invalid")
        _positive_integer(trigger.get("zone_code"), f"{label}.zone_code")
        _positive_integer(trigger.get("visit", 1), f"{label}.visit")
        _finite_number(trigger.get("delay_ms", 0), f"{label}.delay_ms")
        unexpected = set(trigger).difference(
            ("type", "zone_id", "zone_code", "visit", "delay_ms")
        )
    else:
        raise OutageConfigurationError(f"{label}.type must be time or zone_entry")
    if unexpected:
        raise OutageConfigurationError(
            f"{label} contains unsupported keys: {', '.join(sorted(unexpected))}"
        )


def _trigger_identity(trigger: dict[str, Any]) -> tuple[Any, ...]:
    if trigger["type"] == "time":
        return ("time", float(trigger["start_ms"]))
    return (
        "zone_entry",
        int(trigger["zone_code"]),
        int(trigger.get("visit", 1)),
    )


def validate_zone_references(
    config: dict[str, Any],
    schedule: dict[str, Any],
    course_zones: dict[str, Any],
) -> None:
    """Require every outage zone label/code to match the frozen course map."""
    zones = course_zones.get("zones") if isinstance(course_zones, dict) else None
    if not isinstance(zones, list) or not zones:
        raise OutageConfigurationError("course_zones must contain a non-empty zones list")
    code_to_id: dict[int, str] = {}
    for index, zone in enumerate(zones):
        if not isinstance(zone, dict):
            raise OutageConfigurationError(f"course_zones.zones[{index}] is invalid")
        code = zone.get("code")
        zone_id = zone.get("id")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or code <= 0
            or not isinstance(zone_id, str)
        ):
            raise OutageConfigurationError(
                f"course_zones.zones[{index}] has an invalid code or id"
            )
        code_to_id[code] = zone_id

    references: list[tuple[str, dict[str, Any]]] = []
    for index, event in enumerate(config.get("deterministic_events", [])):
        references.append((f"deterministic_events[{index}]", event["trigger"]))
    for index, trigger in enumerate(
        config.get("randomized", {}).get("eligible_triggers", [])
    ):
        references.append((f"randomized.eligible_triggers[{index}]", trigger))
    for index, event in enumerate(schedule.get("events", [])):
        references.append((f"schedule.events[{index}]", event["trigger"]))

    for label, trigger in references:
        if trigger.get("type") != "zone_entry":
            continue
        code = int(trigger["zone_code"])
        expected_id = code_to_id.get(code)
        if expected_id is None:
            raise OutageConfigurationError(f"{label} refers to unknown zone code {code}")
        if trigger.get("zone_id") != expected_id:
            raise OutageConfigurationError(
                f"{label} maps zone code {code} to {trigger.get('zone_id')!r}; "
                f"expected {expected_id!r}"
            )


def materialize_schedule(
    config: dict[str, Any],
    mode: str,
    *,
    condition_override: str | None = None,
    seed_override: int | None = None,
    scripted_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validate_config(config)
    if mode not in ALLOWED_MODES:
        raise OutageConfigurationError(
            f"mode must be one of {', '.join(sorted(ALLOWED_MODES))}"
        )
    if condition_override is not None and condition_override not in ALLOWED_CONDITIONS:
        raise OutageConfigurationError("condition_override must be black or frozen")
    if seed_override is not None and (
        isinstance(seed_override, bool) or not isinstance(seed_override, int)
    ):
        raise OutageConfigurationError("seed_override must be an integer")

    timing = config["timing"]
    control_step_ms = int(timing["control_step_ms"])
    display_period_ms = int(timing["display_period_ms"])
    anchor_period_ms = int(timing["rgbd_anchor_period_ms"])
    seed = int(config["random_seed"] if seed_override is None else seed_override)

    if mode == "off":
        templates: list[dict[str, Any]] = []
    elif mode == "deterministic":
        templates = deepcopy(config["deterministic_events"])
    elif mode == "scripted":
        if not scripted_events:
            raise OutageConfigurationError(
                "scripted mode requires at least one scripted event"
            )
        templates = deepcopy(scripted_events)
        _validate_event_templates(templates)
    else:
        generator = random.Random(seed)
        randomized = config["randomized"]
        triggers = deepcopy(randomized["eligible_triggers"])
        generator.shuffle(triggers)
        triggers = triggers[: int(randomized["event_count"])]
        triggers.sort(
            key=lambda trigger: (
                int(trigger.get("zone_code", 0)),
                float(trigger.get("start_ms", 0)),
            )
        )
        assignments = [
            (condition, duration)
            for duration in timing["durations_ms"]
            for condition in config["conditions"]
        ]
        while len(assignments) < len(triggers):
            assignments.extend(assignments)
        generator.shuffle(assignments)
        templates = [
            {
                "id": f"random_{index + 1:03d}",
                "condition": assignments[index][0],
                "duration_ms": assignments[index][1],
                "trigger": trigger,
            }
            for index, trigger in enumerate(triggers)
        ]

    materialized = []
    for ordinal, template in enumerate(templates, start=1):
        event = deepcopy(template)
        if condition_override is not None:
            event["condition"] = condition_override
        duration = quantize_duration(
            event.pop("duration_ms"),
            control_step_ms=control_step_ms,
            display_period_ms=display_period_ms,
        )
        trigger = event["trigger"]
        if trigger["type"] == "time":
            raw_step = _milliseconds_to_steps_ceil(
                float(trigger["start_ms"]), control_step_ms
            )
            anchor_quantum_steps = anchor_period_ms // control_step_ms
            trigger_step = ceil_to_multiple(
                max(1, raw_step), anchor_quantum_steps
            )
            trigger_step = max(anchor_quantum_steps, trigger_step)
            trigger["planned_start_step"] = trigger_step
            trigger["effective_start_ms"] = trigger_step * control_step_ms
        else:
            delay_steps = _milliseconds_to_steps_ceil(
                float(trigger.get("delay_ms", 0)), control_step_ms
            )
            trigger["delay_control_steps"] = delay_steps
        event.update(duration)
        event["ordinal"] = ordinal
        materialized.append(event)

    _reject_known_time_overlaps(materialized)
    payload = {
        "schema_version": 1,
        "experiment_id": config["experiment_id"],
        "experiment_version": config["version"],
        "mode": mode,
        "seed": seed,
        "condition_override": condition_override,
        "control_step_ms": control_step_ms,
        "display_period_ms": display_period_ms,
        "rgbd_anchor_period_ms": anchor_period_ms,
        "interval_rule": "half-open [start_step, end_step)",
        "events": materialized,
    }
    payload["schedule_sha256"] = canonical_sha256(payload)
    return payload


def _reject_known_time_overlaps(events: list[dict[str, Any]]) -> None:
    intervals = []
    for event in events:
        trigger = event["trigger"]
        if trigger["type"] != "time":
            continue
        start = int(trigger["planned_start_step"])
        end = start + int(event["duration_control_steps"])
        intervals.append((start, end, event["id"]))
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise OutageConfigurationError(
                f"Timed outages overlap: {previous[2]} and {current[2]}"
            )


class OutageRuntime:
    """Resolve a materialized schedule against control steps and zone entries."""

    def __init__(self, schedule: dict[str, Any]) -> None:
        if not isinstance(schedule, dict) or schedule.get("schema_version") != 1:
            raise OutageConfigurationError("Invalid materialized schedule")
        expected_hash = schedule.get("schedule_sha256")
        unhashed = {key: value for key, value in schedule.items() if key != "schedule_sha256"}
        if expected_hash != canonical_sha256(unhashed):
            raise OutageConfigurationError("Materialized schedule hash does not match")
        self.schedule = deepcopy(schedule)
        self.events = self.schedule["events"]
        self.anchor_quantum_steps = (
            int(schedule["rgbd_anchor_period_ms"])
            // int(schedule["control_step_ms"])
        )
        self.current: dict[str, Any] | None = None
        self.pending: list[dict[str, Any]] = []
        self.started_ids: set[str] = set()
        self.completed_ids: set[str] = set()
        self.ended_ids: set[str] = set()
        self.aborted_ids: set[str] = set()
        self.zone_visits: dict[int, int] = {}
        self.previous_zone_code = 0
        self.previous_step = -1

    @property
    def mode(self) -> str:
        return "normal" if self.current is None else str(self.current["condition"])

    @property
    def current_event_id(self) -> str | None:
        return None if self.current is None else str(self.current["id"])

    def abort(self, control_step: int, reason: str) -> dict[str, Any] | None:
        """End the active interval early without moving later triggers."""
        if self.current is None:
            return None
        if control_step < int(self.current["start_step"]):
            raise OutageRuntimeError("An outage cannot abort before it starts")
        event = self.current
        self.current = None
        event["aborted_at_step"] = control_step
        event["abort_reason"] = reason
        self.completed_ids.add(str(event["id"]))
        self.aborted_ids.add(str(event["id"]))
        return {
            "type": "abort",
            "control_step": control_step,
            "event": deepcopy(event),
            "reason": reason,
        }

    def advance(self, control_step: int, zone_code: int) -> list[dict[str, Any]]:
        if control_step <= self.previous_step:
            raise OutageRuntimeError("control steps must increase strictly")
        if control_step < 0:
            raise OutageRuntimeError("control_step must be non-negative")
        transitions: list[dict[str, Any]] = []

        if self.current is not None and control_step >= self.current["end_step"]:
            if control_step != self.current["end_step"]:
                raise OutageRuntimeError(
                    f"Missed exact end step for {self.current['id']}"
                )
            finished = self.current
            self.current = None
            self.completed_ids.add(str(finished["id"]))
            self.ended_ids.add(str(finished["id"]))
            transitions.append(
                {
                    "type": "end",
                    "control_step": control_step,
                    "event": deepcopy(finished),
                }
            )

        entered_zone = zone_code > 0 and zone_code != self.previous_zone_code
        if entered_zone:
            self.zone_visits[zone_code] = self.zone_visits.get(zone_code, 0) + 1

        for event in self.events:
            event_id = str(event["id"])
            if event_id in self.started_ids or any(
                candidate["id"] == event_id for candidate in self.pending
            ):
                continue
            trigger = event["trigger"]
            planned_start = None
            if trigger["type"] == "time":
                if control_step >= int(trigger["planned_start_step"]):
                    planned_start = int(trigger["planned_start_step"])
            elif (
                entered_zone
                and zone_code == int(trigger["zone_code"])
                and self.zone_visits[zone_code] == int(trigger.get("visit", 1))
            ):
                planned_start = ceil_to_multiple(
                    control_step + int(trigger.get("delay_control_steps", 0)),
                    self.anchor_quantum_steps,
                )
            if planned_start is not None:
                pending = deepcopy(event)
                pending["realized_start_step"] = planned_start
                self.pending.append(pending)

        self.pending.sort(key=lambda event: (event["realized_start_step"], event["ordinal"]))
        due = [
            event
            for event in self.pending
            if control_step >= int(event["realized_start_step"])
        ]
        if due:
            if len(due) > 1 or self.current is not None:
                identifiers = [event["id"] for event in due]
                if self.current is not None:
                    identifiers.insert(0, self.current["id"])
                raise OutageRuntimeError(
                    "Realized outages overlap: " + ", ".join(identifiers)
                )
            event = due[0]
            if control_step != int(event["realized_start_step"]):
                raise OutageRuntimeError(f"Missed exact start step for {event['id']}")
            self.pending.remove(event)
            event["start_step"] = control_step
            event["end_step"] = control_step + int(event["duration_control_steps"])
            self.current = event
            self.started_ids.add(str(event["id"]))
            transitions.append(
                {
                    "type": "start",
                    "control_step": control_step,
                    "event": deepcopy(event),
                }
            )

        self.previous_zone_code = zone_code
        self.previous_step = control_step
        return transitions


class PilotFrameSelector:
    """Reference black/frozen/live semantics used for evidence hashes and tests."""

    def __init__(self, frame_size_bytes: int) -> None:
        self.frame_size_bytes = _positive_integer(
            frame_size_bytes, "frame_size_bytes"
        )
        if self.frame_size_bytes % 4 != 0:
            raise OutageConfigurationError(
                "BGRA frame_size_bytes must be divisible by four"
            )
        self.black_frame = bytes((0, 0, 0, 255)) * (self.frame_size_bytes // 4)
        self.anchor: bytes | None = None

    def begin(self, condition: str, live_frame: bytes | None) -> None:
        if condition not in ALLOWED_CONDITIONS:
            raise OutageConfigurationError("condition must be black or frozen")
        if live_frame is not None and len(live_frame) != self.frame_size_bytes:
            raise OutageConfigurationError("live frame has the wrong size")
        # Force a distinct immutable buffer even when the caller already
        # supplied ``bytes``.  This makes the freeze anchor independent of a
        # camera API that may recycle or mutate its backing memory.
        self.anchor = None if live_frame is None else bytes(bytearray(live_frame))

    def select(self, mode: str, live_frame: bytes | None) -> tuple[bytes, str]:
        if live_frame is not None and len(live_frame) != self.frame_size_bytes:
            raise OutageConfigurationError("live frame has the wrong size")
        if mode == "normal":
            if live_frame is None:
                return self.black_frame, "missing_live_frame"
            return bytes(live_frame), "live"
        if mode == "black":
            return self.black_frame, "black"
        if mode == "frozen":
            if self.anchor is None:
                return self.black_frame, "missing_anchor"
            return self.anchor, "anchor"
        raise OutageConfigurationError("mode must be normal, black, or frozen")

    def end(self) -> None:
        self.anchor = None
