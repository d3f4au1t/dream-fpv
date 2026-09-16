import json
import math
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ControllerConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (PROJECT_ROOT / "config" / "controller.json").open(
            encoding="utf-8"
        ) as config_file:
            cls.config = json.load(config_file)

    def test_apex_t19_mode_2_channel_order_is_locked(self):
        self.assertEqual(
            self.config["axes"],
            {"roll": 0, "pitch": 1, "throttle": 2, "yaw": 3},
        )

    def test_frozen_apparatus_files_are_explicit(self):
        self.assertEqual(
            self.config["apparatus_manifest"],
            "config/apparatus_v1.json",
        )
        self.assertEqual(
            self.config["course_zones_file"],
            "config/course_zones.json",
        )

    def test_user_confirmed_axis_inversions_are_locked(self):
        self.assertEqual(
            self.config["invert"],
            {"roll": True, "pitch": True, "throttle": False, "yaw": True},
        )

    def test_accepted_rate_profile_is_locked(self):
        profile = self.config["flight_profile"]
        self.assertEqual(profile["rate_type"], "betaflight")
        self.assertEqual(
            profile["rates"],
            {
                "roll": {"rc_rate": 1.25, "super_rate": 0.68, "expo": 0.22},
                "pitch": {"rc_rate": 1.25, "super_rate": 0.68, "expo": 0.22},
                "yaw": {"rc_rate": 1.25, "super_rate": 0.55, "expo": 0.28},
            },
        )

    def test_uncrashed_airframe_reference_is_locked(self):
        reference = self.config["flight_profile"]["reference_airframe"]
        self.assertEqual(reference["fpv_camera_angle_deg"], 22.0)
        self.assertEqual(reference["propeller_size_in"], 2.5)
        self.assertEqual(reference["motor_kv"], 5000)
        self.assertEqual(reference["battery_cells"], 4)
        self.assertEqual(reference["propeller_pitch_in"], 2.0)
        self.assertEqual(reference["mass_g"], 198)
        self.assertEqual(reference["minimum_throttle_percent"], 8.0)
        self.assertEqual(reference["gravity_m_s2"], 9.82)
        self.assertEqual(reference["air_friction"], 0.55)
        self.assertEqual(reference["air_grip"], 0.48)
        self.assertEqual(
            reference["propulsion_calibration"][
                "maximum_static_thrust_g_per_motor"
            ],
            255,
        )

    def test_radio_failsafe_timeout_is_bounded(self):
        timeout = self.config["joystick_stale_timeout_seconds"]
        self.assertGreater(timeout, 0.0)
        self.assertLessEqual(timeout, 0.25)

    def test_fault_rearm_requires_a_deliberate_throttle_cycle(self):
        profile = self.config["flight_profile"]
        self.assertGreater(
            profile["rearm_reset_high_throttle"],
            profile["arm_throttle_max"],
        )
        self.assertLessEqual(profile["rearm_reset_high_throttle"], 0.15)

    def test_boundary_rearm_requires_centered_controls(self):
        profile = self.config["flight_profile"]
        self.assertGreaterEqual(profile["boundary_rearm_stick_max"], 0.05)
        self.assertLessEqual(profile["boundary_rearm_stick_max"], 0.15)
        self.assertGreaterEqual(profile["boundary_rearm_neutral_seconds"], 0.10)
        self.assertLessEqual(profile["boundary_rearm_neutral_seconds"], 0.25)
        self.assertGreaterEqual(profile["crash_recovery_tilt_deg"], 60.0)
        self.assertLessEqual(profile["crash_recovery_tilt_deg"], 90.0)
        self.assertGreaterEqual(profile["crash_recovery_seconds"], 0.5)
        self.assertLessEqual(profile["crash_recovery_seconds"], 1.5)

    def test_recovery_bounds_are_finite_and_well_ordered(self):
        bounds = self.config["flight_profile"]["recovery_bounds"]
        for axis in ("x_m", "y_m", "z_m"):
            lower, upper = bounds[axis]
            self.assertTrue(math.isfinite(lower))
            self.assertTrue(math.isfinite(upper))
            self.assertLess(lower, upper)
        self.assertGreaterEqual(bounds["x_m"][1] - bounds["x_m"][0], 30.0)
        self.assertGreaterEqual(bounds["y_m"][1] - bounds["y_m"][0], 16.0)
        self.assertLess(bounds["z_m"][0], 0.03)
        self.assertGreater(bounds["z_m"][1], 3.0)

    def test_runtime_logging_is_bounded(self):
        self.assertFalse(self.config["continuous_research_sensors"])
        self.assertGreaterEqual(self.config["telemetry_segment_bytes"], 1024 * 1024)
        self.assertLessEqual(self.config["telemetry_segment_bytes"], 128 * 1024 * 1024)
        self.assertGreaterEqual(self.config["telemetry_max_segments"], 1)
        self.assertLessEqual(self.config["telemetry_max_segments"], 8)


if __name__ == "__main__":
    unittest.main()
