import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from outage_baselines import (  # noqa: E402
        OutageConfigurationError,
        OutageRuntime,
        OutageRuntimeError,
        PilotFrameSelector,
        materialize_schedule,
        quantize_duration,
        validate_config,
        validate_zone_references,
    )
finally:
    sys.path.remove(str(CONTROLLER_DIR))


class OutageBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads(
            (PROJECT_ROOT / "config" / "outage_baselines_v1.json").read_text(
                encoding="utf-8"
            )
        )

    def test_repository_configuration_is_valid(self):
        validate_config(self.config)

    def test_required_durations_quantize_to_display_frames(self):
        expected = {
            100: (7, 14, 112),
            250: (16, 32, 256),
            500: (32, 64, 512),
            750: (47, 94, 752),
            1000: (63, 126, 1008),
        }
        for requested, values in expected.items():
            result = quantize_duration(
                requested,
                control_step_ms=8,
                display_period_ms=16,
            )
            self.assertEqual(
                (
                    result["duration_display_frames"],
                    result["duration_control_steps"],
                    result["effective_duration_ms"],
                ),
                values,
            )
            self.assertGreaterEqual(result["quantization_error_ms"], 0)
            self.assertLess(result["quantization_error_ms"], 16)

    def test_deterministic_schedule_is_stable_and_balanced(self):
        first = materialize_schedule(self.config, "deterministic")
        second = materialize_schedule(self.config, "deterministic")
        self.assertEqual(first, second)
        self.assertEqual(len(first["schedule_sha256"]), 64)
        self.assertEqual(len(first["events"]), 10)
        self.assertEqual(
            [event["condition"] for event in first["events"]].count("black"),
            5,
        )
        self.assertEqual(
            [event["condition"] for event in first["events"]].count("frozen"),
            5,
        )
        requested = [event["requested_duration_ms"] for event in first["events"]]
        for duration in (100, 250, 500, 750, 1000):
            self.assertEqual(requested.count(duration), 2)

    def test_seeded_random_schedule_replays_and_changes_with_seed(self):
        first = materialize_schedule(self.config, "randomized", seed_override=42)
        replay = materialize_schedule(self.config, "randomized", seed_override=42)
        changed = materialize_schedule(self.config, "randomized", seed_override=43)
        self.assertEqual(first, replay)
        self.assertNotEqual(first["schedule_sha256"], changed["schedule_sha256"])
        self.assertEqual(len(first["events"]), 6)
        first_zones = [event["trigger"]["zone_code"] for event in first["events"]]
        changed_zones = [
            event["trigger"]["zone_code"] for event in changed["events"]
        ]
        self.assertEqual(first_zones, sorted(set(first_zones)))
        self.assertNotEqual(first_zones, changed_zones)

    def test_condition_override_produces_single_condition_trials(self):
        schedule = materialize_schedule(
            self.config,
            "deterministic",
            condition_override="frozen",
        )
        self.assertEqual(
            {event["condition"] for event in schedule["events"]},
            {"frozen"},
        )

    def test_invalid_configuration_and_overlapping_time_script_are_rejected(self):
        invalid = json.loads(json.dumps(self.config))
        invalid["timing"]["display_period_ms"] = 15
        with self.assertRaises(OutageConfigurationError):
            validate_config(invalid)

        script = [
            {
                "id": "script_001",
                "condition": "black",
                "duration_ms": 500,
                "trigger": {"type": "time", "start_ms": 128},
            },
            {
                "id": "script_002",
                "condition": "frozen",
                "duration_ms": 250,
                "trigger": {"type": "time", "start_ms": 256},
            },
        ]
        with self.assertRaises(OutageConfigurationError):
            materialize_schedule(
                self.config,
                "scripted",
                scripted_events=script,
            )

    def test_time_trigger_uses_exact_half_open_interval(self):
        script = [
            {
                "id": "script_001",
                "condition": "black",
                "duration_ms": 100,
                "trigger": {"type": "time", "start_ms": 65},
            }
        ]
        schedule = materialize_schedule(
            self.config,
            "scripted",
            scripted_events=script,
        )
        event = schedule["events"][0]
        self.assertEqual(event["trigger"]["planned_start_step"], 16)
        runtime = OutageRuntime(schedule)
        transitions = []
        modes = {}
        for step in range(1, 31):
            transitions.extend(runtime.advance(step, 0))
            modes[step] = runtime.mode
        self.assertEqual(
            [(item["type"], item["control_step"]) for item in transitions],
            [("start", 16), ("end", 30)],
        )
        self.assertEqual(modes[15], "normal")
        self.assertEqual(modes[16], "black")
        self.assertEqual(modes[29], "black")
        self.assertEqual(modes[30], "normal")

    def test_zero_time_trigger_starts_at_first_rgbd_boundary(self):
        script = [
            {
                "id": "script_001",
                "condition": "black",
                "duration_ms": 100,
                "trigger": {"type": "time", "start_ms": 0},
            }
        ]
        schedule = materialize_schedule(
            self.config,
            "scripted",
            scripted_events=script,
        )
        self.assertEqual(
            schedule["events"][0]["trigger"]["planned_start_step"],
            8,
        )
        runtime = OutageRuntime(schedule)
        transitions = []
        for step in range(1, 23):
            transitions.extend(runtime.advance(step, 0))
        self.assertEqual(
            [(item["type"], item["control_step"]) for item in transitions],
            [("start", 8), ("end", 22)],
        )

    def test_zone_entry_waits_for_rgbd_boundary_and_does_not_retrigger(self):
        config = json.loads(json.dumps(self.config))
        config["deterministic_events"] = [
            {
                "id": "zone_001",
                "condition": "frozen",
                "duration_ms": 100,
                "trigger": {
                    "type": "zone_entry",
                    "zone_id": "occlusion_turn",
                    "zone_code": 2,
                    "visit": 1,
                    "delay_ms": 10,
                },
            }
        ]
        schedule = materialize_schedule(config, "deterministic")
        runtime = OutageRuntime(schedule)
        transitions = []
        for step in range(1, 40):
            zone = 2 if 3 <= step <= 5 or 30 <= step <= 35 else 0
            transitions.extend(runtime.advance(step, zone))
        self.assertEqual(
            [(item["type"], item["control_step"]) for item in transitions],
            [("start", 8), ("end", 22)],
        )

    def test_runtime_rejects_realized_overlap(self):
        config = json.loads(json.dumps(self.config))
        config["deterministic_events"] = [
            {
                "id": "zone_001",
                "condition": "black",
                "duration_ms": 1000,
                "trigger": {
                    "type": "zone_entry",
                    "zone_id": "start_straight",
                    "zone_code": 1,
                    "visit": 1,
                    "delay_ms": 0,
                },
            },
            {
                "id": "zone_002",
                "condition": "frozen",
                "duration_ms": 100,
                "trigger": {
                    "type": "zone_entry",
                    "zone_id": "occlusion_turn",
                    "zone_code": 2,
                    "visit": 1,
                    "delay_ms": 0,
                },
            },
        ]
        schedule = materialize_schedule(config, "deterministic")
        runtime = OutageRuntime(schedule)
        for step in range(1, 8):
            runtime.advance(step, 1)
        with self.assertRaises(OutageRuntimeError):
            runtime.advance(8, 2)

    def test_abort_is_counted_separately_from_normal_completion(self):
        script = [
            {
                "id": "script_001",
                "condition": "black",
                "duration_ms": 100,
                "trigger": {"type": "time", "start_ms": 64},
            }
        ]
        runtime = OutageRuntime(
            materialize_schedule(
                self.config,
                "scripted",
                scripted_events=script,
            )
        )
        for step in range(1, 9):
            runtime.advance(step, 0)
        transition = runtime.abort(9, "test")
        self.assertEqual(transition["type"], "abort")
        self.assertEqual(runtime.completed_ids, {"script_001"})
        self.assertEqual(runtime.aborted_ids, {"script_001"})
        self.assertEqual(runtime.ended_ids, set())

    def test_zone_references_must_match_the_course_map(self):
        zones = json.loads(
            (PROJECT_ROOT / "config" / "course_zones.json").read_text(
                encoding="utf-8"
            )
        )
        schedule = materialize_schedule(self.config, "deterministic")
        validate_zone_references(self.config, schedule, zones)

        invalid = json.loads(json.dumps(self.config))
        invalid["deterministic_events"][0]["trigger"]["zone_id"] = "finish"
        with self.assertRaises(OutageConfigurationError):
            validate_zone_references(
                invalid,
                materialize_schedule(invalid, "deterministic"),
                zones,
            )

    def test_pilot_frame_selector_holds_an_immutable_anchor(self):
        selector = PilotFrameSelector(8)
        first = bytes((1, 2, 3, 255, 4, 5, 6, 255))
        second = bytes((7, 8, 9, 255, 10, 11, 12, 255))
        selector.begin("frozen", first)
        frozen, source = selector.select("frozen", second)
        self.assertEqual(frozen, first)
        self.assertEqual(source, "anchor")
        self.assertIsNot(frozen, first)
        live, source = selector.select("normal", second)
        self.assertEqual(live, second)
        self.assertEqual(source, "live")

    def test_black_and_missing_anchor_fail_closed(self):
        selector = PilotFrameSelector(8)
        black, source = selector.select("black", b"\x01" * 8)
        self.assertEqual(black, bytes((0, 0, 0, 255)) * 2)
        self.assertEqual(source, "black")
        selector.begin("frozen", None)
        fallback, source = selector.select("frozen", b"\x01" * 8)
        self.assertEqual(fallback, black)
        self.assertEqual(source, "missing_anchor")


if __name__ == "__main__":
    unittest.main()
