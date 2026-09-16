import sys
from pathlib import Path
import unittest


CONTROLLER_DIR = (
    Path(__file__).resolve().parents[1] / "controllers" / "dream_mode_controller"
)
sys.path.insert(0, str(CONTROLLER_DIR))

from input_mapping import (  # noqa: E402
    actual_rate_setpoint,
    betaflight_rate_setpoint,
    desaturate_motor_mix,
    hid_byte_to_signed,
    normalize_axis,
    normalize_throttle,
    shape_throttle,
    throttle_to_motor_speed,
    thrust_mix_to_motor_speeds,
)


class HidByteTests(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(hid_byte_to_signed(0), -32767)
        self.assertEqual(hid_byte_to_signed(255), 32767)

    def test_center_is_close_to_zero(self):
        self.assertLess(abs(hid_byte_to_signed(127)), 200)
        self.assertLess(abs(hid_byte_to_signed(128)), 200)

    def test_rejects_invalid_input(self):
        with self.assertRaises(ValueError):
            hid_byte_to_signed(-1)
        with self.assertRaises(ValueError):
            hid_byte_to_signed(256)


class NormalizeAxisTests(unittest.TestCase):
    def test_endpoints_and_center(self):
        self.assertEqual(normalize_axis(-32767), -1.0)
        self.assertEqual(normalize_axis(0), 0.0)
        self.assertEqual(normalize_axis(32767), 1.0)

    def test_clamps_device_overflow(self):
        self.assertEqual(normalize_axis(-40000), -1.0)
        self.assertEqual(normalize_axis(40000), 1.0)

    def test_inversion(self):
        self.assertAlmostEqual(normalize_axis(16384, invert=True), -16384 / 32767)

    def test_scaled_deadzone(self):
        self.assertEqual(normalize_axis(100, deadzone=0.04), 0.0)
        self.assertAlmostEqual(normalize_axis(32767, deadzone=0.04), 1.0)

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            normalize_axis(0, axis_range=0)
        with self.assertRaises(ValueError):
            normalize_axis(0, deadzone=1.0)


class NormalizeThrottleTests(unittest.TestCase):
    def test_endpoints_and_center(self):
        self.assertEqual(normalize_throttle(-32767), 0.0)
        self.assertEqual(normalize_throttle(0), 0.5)
        self.assertEqual(normalize_throttle(32767), 1.0)

    def test_inversion(self):
        self.assertEqual(normalize_throttle(-32767, invert=True), 1.0)
        self.assertEqual(normalize_throttle(32767, invert=True), 0.0)

    def test_throttle_curve_preserves_endpoints(self):
        self.assertEqual(shape_throttle(0.0, 1.24), 0.0)
        self.assertEqual(shape_throttle(1.0, 1.24), 1.0)
        self.assertLess(shape_throttle(0.5, 1.24), 0.5)

    def test_throttle_curve_rejects_invalid_exponent(self):
        with self.assertRaises(ValueError):
            shape_throttle(0.5, 0.0)


class ActualRatesTests(unittest.TestCase):
    def test_full_stick_reaches_configured_rate(self):
        settings = {
            "center_rate_deg_s": 150,
            "max_rate_deg_s": 910,
            "expo": 0.76,
        }
        self.assertAlmostEqual(actual_rate_setpoint(1.0, **settings), 910.0)
        self.assertAlmostEqual(actual_rate_setpoint(-1.0, **settings), -910.0)

    def test_center_slope_matches_center_sensitivity(self):
        rate = actual_rate_setpoint(0.000001, 150, 910, 0.76)
        self.assertAlmostEqual(rate / 0.000001, 150.0, places=2)

    def test_curve_is_odd_and_clamps_stick(self):
        positive = actual_rate_setpoint(0.6, 150, 910, 0.76)
        negative = actual_rate_setpoint(-0.6, 150, 910, 0.76)
        self.assertAlmostEqual(negative, -positive)
        self.assertEqual(actual_rate_setpoint(2.0, 150, 910, 0.76), 910.0)

    def test_rejects_invalid_rate_settings(self):
        with self.assertRaises(ValueError):
            actual_rate_setpoint(0, -1, 910, 0.5)
        with self.assertRaises(ValueError):
            actual_rate_setpoint(0, 200, 100, 0.5)
        with self.assertRaises(ValueError):
            actual_rate_setpoint(0, 100, 900, 1.1)


class BetaflightRatesTests(unittest.TestCase):
    def test_screenshot_roll_and_pitch_curve(self):
        settings = {"rc_rate": 1.25, "super_rate": 0.68, "expo": 0.22}
        expected = {
            0.25: 58.99378765,
            0.50: 152.93560606,
            0.75: 333.984375,
            1.00: 781.25,
        }
        for stick, rate in expected.items():
            self.assertAlmostEqual(
                betaflight_rate_setpoint(stick, **settings), rate
            )

    def test_screenshot_yaw_curve(self):
        settings = {"rc_rate": 1.25, "super_rate": 0.55, "expo": 0.28}
        expected = {
            0.25: 52.49094203,
            0.50: 130.17241379,
            0.75: 267.48670213,
            1.00: 555.55555556,
        }
        for stick, rate in expected.items():
            self.assertAlmostEqual(
                betaflight_rate_setpoint(stick, **settings), rate
            )

    def test_curve_is_odd_and_clamps_stick(self):
        positive = betaflight_rate_setpoint(0.6, 1.25, 0.68, 0.22)
        self.assertAlmostEqual(
            betaflight_rate_setpoint(-0.6, 1.25, 0.68, 0.22), -positive
        )
        self.assertAlmostEqual(
            betaflight_rate_setpoint(2.0, 1.25, 0.68, 0.22), 781.25
        )

    def test_high_rc_rate_branch_and_invalid_settings(self):
        self.assertAlmostEqual(
            betaflight_rate_setpoint(1.0, 2.1, 0.0, 0.0), 710.8
        )
        with self.assertRaises(ValueError):
            betaflight_rate_setpoint(0.0, -0.1, 0.5, 0.2)
        with self.assertRaises(ValueError):
            betaflight_rate_setpoint(0.0, 1.0, 1.0, 0.2)
        with self.assertRaises(ValueError):
            betaflight_rate_setpoint(0.0, 1.0, 0.5, 1.1)


class MotorMappingTests(unittest.TestCase):
    def test_throttle_endpoints(self):
        self.assertEqual(throttle_to_motor_speed(0.0, 15.0, 170.0), 15.0)
        self.assertEqual(throttle_to_motor_speed(1.0, 15.0, 170.0), 170.0)

    def test_half_throttle_commands_half_available_thrust(self):
        speed = throttle_to_motor_speed(0.5, 15.0, 170.0)
        thrust_fraction = (speed**2 - 15.0**2) / (170.0**2 - 15.0**2)
        self.assertAlmostEqual(thrust_fraction, 0.5)

    def test_desaturation_preserves_mix_inside_bounds(self):
        corrections = (-20.0, 20.0, 10.0, -10.0)
        motors = desaturate_motor_mix(165.0, corrections, 15.0, 170.0)
        self.assertTrue(all(15.0 <= value <= 170.0 for value in motors))
        self.assertAlmostEqual(motors[1] - motors[0], 40.0)

    def test_desaturation_scales_impossible_mix(self):
        motors = desaturate_motor_mix(
            90.0,
            (-200.0, 200.0, 100.0, -100.0),
            15.0,
            170.0,
        )
        self.assertEqual(min(motors), 15.0)
        self.assertEqual(max(motors), 170.0)

    def test_thrust_domain_mix_is_independent_of_base_throttle(self):
        idle = 15.0
        maximum = 135.0
        thrust_span = maximum**2 - idle**2

        def normalized_thrust(speed):
            return (speed**2 - idle**2) / thrust_span

        corrections = (0.04, -0.04, 0.04, -0.04)
        low = thrust_mix_to_motor_speeds(0.15, corrections, idle, maximum)
        high = thrust_mix_to_motor_speeds(0.65, corrections, idle, maximum)

        low_difference = normalized_thrust(low[0]) - normalized_thrust(low[1])
        high_difference = normalized_thrust(high[0]) - normalized_thrust(high[1])
        self.assertAlmostEqual(low_difference, 0.08)
        self.assertAlmostEqual(high_difference, 0.08)
        self.assertAlmostEqual(low_difference, high_difference)


if __name__ == "__main__":
    unittest.main()
