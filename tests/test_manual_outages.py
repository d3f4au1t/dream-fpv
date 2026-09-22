"""Manual key edges, exact outage timing, recovery and HID coexistence."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from test_controller_failsafe import DreamModeController, FakeJoystick, PROJECT_ROOT
from outage_baselines import OutageConfigurationError, OutageRuntime, materialize_schedule, validate_config


class ManualOutageTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(
            (PROJECT_ROOT / "config/outage_baselines_v1.json").read_text()
        )
        self.schedule = materialize_schedule(self.config, "manual")
        self.runtime = OutageRuntime(self.schedule)

    def controller(self, runtime=None):
        controller = DreamModeController.__new__(DreamModeController)
        controller.outage_runtime = runtime or self.runtime
        controller.manual_outage_keys_down = set()
        controller.test_outage_key_ranges = []
        controller.throttle = 0.323
        controller.step_count = 1
        controller.keyboard = Mock()
        controller._write_outage_event = Mock()
        return controller

    def sample(self, controller, keys):
        controller.keyboard.getKey.side_effect = [*(ord(key) for key in keys), -1]
        result = controller._read_keyboard()
        transitions = controller.outage_runtime.advance(controller.step_count, 0)
        controller.step_count += 1
        return result, transitions

    def test_three_distinct_durations_end_on_exact_steps(self):
        for key, duration in (("1", 256), ("2", 512), ("3", 1008)):
            with self.subTest(key=key):
                runtime = OutageRuntime(self.schedule)
                event = runtime.request_manual(key, 3)
                self.assertEqual(event["trigger"]["planned_start_step"], 8)
                self.assertEqual(event["effective_duration_ms"], duration)
                transitions = []
                for step in range(1, 9 + duration // 8):
                    transitions.extend(runtime.advance(step, 0))
                self.assertEqual(
                    [(t["type"], t["control_step"]) for t in transitions],
                    [("start", 8), ("end", 8 + duration // 8)],
                )
                self.assertEqual(runtime.mode, "normal")
                self.assertEqual(runtime.schedule, self.schedule)
                self.assertEqual(self.schedule["events"], [])

    def test_no_automatic_outages_without_a_key(self):
        for step in range(1, 1500):
            self.assertEqual(self.runtime.advance(step, step % 11), [])
        self.assertEqual(self.runtime.mode, "normal")

    def test_requests_while_pending_or_active_are_not_queued(self):
        self.assertIsNotNone(self.runtime.request_manual("1", 1))
        self.assertIsNone(self.runtime.request_manual("3", 2))
        for step in range(1, 10):
            self.runtime.advance(step, 0)
        self.assertIsNone(self.runtime.request_manual("2", 10))
        for step in range(10, 50):
            self.runtime.advance(step, 0)
        self.assertEqual(len(self.runtime.events), 1)
        self.assertEqual(self.runtime.pending, [])
        self.assertIsNotNone(self.runtime.request_manual("3", 50))

    def test_held_key_only_fires_once_until_released(self):
        controller = self.controller()
        for _ in range(100):
            (commands, active), _ = self.sample(controller, "1")
            self.assertEqual(commands, (0, 0, 0, 0.323))
            self.assertFalse(active)
        self.assertEqual(len(self.runtime.events), 1)
        self.assertEqual(self.runtime.mode, "normal")
        self.sample(controller, "")
        self.sample(controller, "1")
        self.assertEqual(len(self.runtime.events), 2)
        self.assertEqual(controller._write_outage_event.call_count, 2)

    def test_key_pressed_during_outage_and_held_past_return_is_consumed(self):
        controller = self.controller()
        self.sample(controller, "1")
        for _ in range(100):
            self.sample(controller, "3")
        self.assertEqual(len(self.runtime.events), 1)
        self.sample(controller, "")
        self.sample(controller, "3")
        self.assertEqual(len(self.runtime.events), 2)

    def test_simultaneous_keys_start_only_shortest_numbered_key(self):
        controller = self.controller()
        self.sample(controller, "321")
        self.assertEqual(len(self.runtime.events), 1)
        self.assertEqual(self.runtime.events[0]["trigger"]["key"], "1")

    def test_acceptance_key_samples_use_same_edge_path(self):
        controller = self.controller()
        controller.test_outage_key_ranges = [[1, 100, "1"]]
        for _ in range(110):
            self.sample(controller, "")
        self.assertEqual(len(self.runtime.events), 1)
        self.assertEqual(self.runtime.ended_ids, {"manual_001"})

    def test_pending_outage_is_cancelled_on_recovery(self):
        controller = self.controller()
        self.sample(controller, "3")
        controller._finish_outage = Mock()
        controller._abort_active_outage("vehicle_recovery")
        controller._finish_outage.assert_not_called()
        self.assertEqual(controller._write_outage_event.call_args.args[0], "cancel")
        for _ in range(140):
            self.sample(controller, "3")
        self.assertEqual(self.runtime.started_ids, set())
        self.assertEqual(self.runtime.cancelled_ids, {"manual_001"})

    def test_active_outage_uses_existing_live_restore_on_recovery(self):
        controller = self.controller()
        for _ in range(8):
            self.sample(controller, "3")
        controller._finish_outage = Mock()
        controller._abort_active_outage("vehicle_recovery")
        controller._finish_outage.assert_called_once()
        self.assertEqual(controller._finish_outage.call_args.kwargs, {"abort_reason": "vehicle_recovery"})
        self.assertEqual(self.runtime.mode, "normal")
        self.assertEqual(self.runtime.aborted_ids, {"manual_001"})

    def test_manual_keys_cannot_modify_off_or_formal_schedules(self):
        for mode in ("off", "deterministic", "randomized"):
            runtime = OutageRuntime(materialize_schedule(self.config, mode))
            controller = self.controller(runtime)
            before = deepcopy(runtime.events)
            self.sample(controller, "123")
            self.assertEqual(runtime.events, before)
            controller._write_outage_event.assert_not_called()

    def test_condition_override_supports_frozen_manual_outages(self):
        runtime = OutageRuntime(materialize_schedule(self.config, "manual", condition_override="frozen"))
        self.assertEqual(runtime.request_manual("1", 1)["condition"], "frozen")

    def test_invalid_manual_bindings_are_rejected(self):
        for bindings in ({"1": 250}, {"1": 250, "2": 250, "3": 500},
                         {"1": True, "2": 500, "3": 1000},
                         {"1": 123, "2": 500, "3": 1000}):
            config = deepcopy(self.config)
            config["manual"]["key_durations_ms"] = bindings
            with self.assertRaises(OutageConfigurationError):
                validate_config(config)

    def test_manual_key_does_not_replace_connected_radio_input(self):
        controller = self.controller()
        controller.config = json.loads((PROJECT_ROOT / "config/controller.json").read_text())
        controller._refresh_joystick = lambda: None
        controller.joystick = FakeJoystick([[180, 128, 90, 160]])
        controller.keyboard.getKey.side_effect = [ord("2"), -1]
        commands, axes = controller._read_inputs()
        self.assertEqual(controller.current_input_source, "joystick")
        self.assertTrue(controller.input_can_arm)
        self.assertNotEqual(commands[0], 0)
        self.assertNotEqual(commands[2], 0)
        self.assertNotEqual(commands[3], controller.throttle)
        self.assertEqual(len(axes), controller.MAX_RAW_AXES)
        self.assertEqual(self.runtime.pending[0]["requested_duration_ms"], 500)


if __name__ == "__main__":
    unittest.main()
