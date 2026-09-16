import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"


def load_controller_module():
    webots_module = types.ModuleType("controller")

    class Keyboard:
        LEFT = 1
        RIGHT = 2
        UP = 3
        DOWN = 4

    class Supervisor:
        pass

    webots_module.Keyboard = Keyboard
    webots_module.Supervisor = Supervisor
    hid_module = types.ModuleType("hid")
    hid_module.enumerate = lambda: []
    hid_module.device = object
    sys.modules.setdefault("controller", webots_module)
    sys.modules.setdefault("hid", hid_module)
    sys.path.insert(0, str(CONTROLLER_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "dream_mode_controller_under_test",
            CONTROLLER_DIR / "dream_mode_controller.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(CONTROLLER_DIR))


CONTROLLER_MODULE = load_controller_module()
DreamModeController = CONTROLLER_MODULE.DreamModeController


class FakeJoystick:
    def __init__(self, reports):
        self.reports = list(reports)
        self.closed = False

    def read(self, _size):
        return self.reports.pop(0) if self.reports else []

    def close(self):
        self.closed = True


class FakeMotor:
    def __init__(self):
        self.velocity = None

    def setVelocity(self, velocity):
        self.velocity = velocity


class FakeField:
    def __init__(self, value):
        self.value = value

    def getSFVec3f(self):
        return self.value

    def getSFRotation(self):
        return self.value

    def getSFString(self):
        return self.value

    def getSFFloat(self):
        return self.value

    def setSFVec3f(self, value):
        self.value = list(value)

    def setSFRotation(self, value):
        self.value = list(value)

    def setSFString(self, value):
        self.value = value

    def setSFFloat(self, value):
        self.value = value


class FakeNode:
    def __init__(self, fields):
        self.fields = {name: FakeField(value) for name, value in fields.items()}
        self.physics_reset = False

    def getField(self, name):
        return self.fields[name]

    def resetPhysics(self):
        self.physics_reset = True


class ControllerFailsafeTests(unittest.TestCase):
    def test_short_hid_report_becomes_invalid_input(self):
        controller = DreamModeController.__new__(DreamModeController)
        controller._read_keyboard = lambda: ((0.0, 0.0, 0.0, 0.0), False)
        controller._refresh_joystick = lambda: None
        controller.joystick = FakeJoystick([[128, 128]])
        controller.joystick_report = []
        controller.joystick_report_wall_time = None
        controller.config = {
            "axis_range": 32767,
            "deadzone": 0.02,
            "joystick_stale_timeout_seconds": 0.25,
            "axes": {"roll": 0, "pitch": 1, "throttle": 2, "yaw": 3},
            "invert": {
                "roll": True,
                "pitch": True,
                "throttle": False,
                "yaw": True,
            },
        }
        controller.current_input_source = "joystick"
        controller.input_can_arm = True

        with redirect_stdout(io.StringIO()):
            commands, raw_axes = controller._read_inputs()

        self.assertEqual(commands, (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(raw_axes, [128, 128, 0, 0, 0, 0, 0, 0])
        self.assertEqual(controller.current_input_source, "invalid")
        self.assertFalse(controller.input_can_arm)

    def test_joystick_without_first_report_is_closed_and_rescanned(self):
        controller = DreamModeController.__new__(DreamModeController)
        controller._read_keyboard = lambda: ((0.0, 0.0, 0.0, 0.0), False)
        controller._refresh_joystick = lambda: None
        joystick = FakeJoystick([])
        controller.joystick = joystick
        controller.joystick_report = []
        controller.joystick_report_wall_time = None
        controller.joystick_open_wall_time = 10.0
        controller.config = {"joystick_stale_timeout_seconds": 0.25}
        controller.current_input_source = "joystick"
        controller.input_can_arm = True

        with patch.object(CONTROLLER_MODULE.time, "monotonic", return_value=10.5):
            with redirect_stdout(io.StringIO()):
                commands, _ = controller._read_inputs()

        self.assertEqual(commands, (0.0, 0.0, 0.0, 0.0))
        self.assertTrue(joystick.closed)
        self.assertIsNone(controller.joystick)
        self.assertIsNone(controller.joystick_open_wall_time)
        self.assertEqual(controller.current_input_source, "invalid")
        self.assertFalse(controller.input_can_arm)

    def test_repeated_malformed_reports_close_bad_hid_interface(self):
        controller = DreamModeController.__new__(DreamModeController)
        controller._read_keyboard = lambda: ((0.0, 0.0, 0.0, 0.0), False)
        controller._refresh_joystick = lambda: None
        joystick = FakeJoystick([[128, 128]])
        controller.joystick = joystick
        controller.joystick_report = []
        controller.joystick_report_wall_time = None
        controller.joystick_open_wall_time = 10.0
        controller.joystick_malformed_since = 10.0
        controller.joystick_path = b"malformed-interface"
        controller.last_failed_joystick_path = None
        controller.config = {
            "axis_range": 32767,
            "deadzone": 0.02,
            "joystick_stale_timeout_seconds": 0.25,
            "axes": {"roll": 0, "pitch": 1, "throttle": 2, "yaw": 3},
            "invert": {
                "roll": True,
                "pitch": True,
                "throttle": False,
                "yaw": True,
            },
        }
        controller.current_input_source = "joystick"
        controller.input_can_arm = True

        with patch.object(CONTROLLER_MODULE.time, "monotonic", return_value=10.5):
            with redirect_stdout(io.StringIO()):
                commands, _ = controller._read_inputs()

        self.assertEqual(commands, (0.0, 0.0, 0.0, 0.0))
        self.assertTrue(joystick.closed)
        self.assertIsNone(controller.joystick)
        self.assertEqual(
            controller.last_failed_joystick_path,
            b"malformed-interface",
        )
        self.assertEqual(controller.current_input_source, "invalid")
        self.assertFalse(controller.input_can_arm)

    def test_best_effort_motor_stop_continues_after_one_failure(self):
        class FailingMotor:
            def setVelocity(self, _velocity):
                raise RuntimeError("motor unavailable")

        controller = DreamModeController.__new__(DreamModeController)
        healthy = FakeMotor()
        controller.motors = {"failing": FailingMotor(), "healthy": healthy}

        controller._stop_motors(best_effort=True)

        self.assertEqual(healthy.velocity, 0.0)

    def test_later_recovery_preserves_stronger_existing_latch(self):
        controller = DreamModeController.__new__(DreamModeController)
        controller.motors = {"motor": FakeMotor()}
        controller.armed = False
        controller.airborne = True
        controller.ground_contact_since = 1.0
        controller.rate_integral = [1.0, 1.0, 1.0]
        controller.last_motor_speeds = (1.0, 1.0, 1.0, 1.0)
        controller.last_commanded_thrust = 1.0
        controller.last_rate_setpoints_deg_s = (1.0, 1.0, 1.0)
        controller.armed_input_source = None
        controller.rearm_inhibit = True
        controller.rearm_source_required = "joystick"
        controller.rearm_high_seen = True
        controller.boundary_rearm_pending = False
        controller.boundary_neutral_since = None
        controller.explicit_arm_required = True
        controller.manual_arm_requested = True
        controller.disarm_event_count = 1
        controller.last_disarm_reason_code = 2

        controller._disarm("", reason_code=4)

        self.assertEqual(controller.rearm_source_required, "joystick")
        self.assertTrue(controller.explicit_arm_required)
        self.assertFalse(controller.rearm_high_seen)
        self.assertFalse(controller.boundary_rearm_pending)
        self.assertIsNone(controller.boundary_neutral_since)
        self.assertFalse(controller.manual_arm_requested)
        self.assertEqual(controller.last_disarm_reason_code, 4)
        self.assertEqual(controller.disarm_event_count, 2)

    def test_home_recovery_preserves_source_and_restores_fpv_mode(self):
        controller = DreamModeController.__new__(DreamModeController)
        motor = FakeMotor()
        controller.motors = {"motor": motor}
        controller.drone_node = FakeNode(
            {
                "translation": [8.0, 4.0, 2.0],
                "rotation": [1.0, 0.0, 0.0, 3.14],
            }
        )
        controller.viewpoint_node = FakeNode(
            {
                "position": [2.0, 2.0, 2.0],
                "orientation": [1.0, 0.0, 0.0, 1.0],
                "follow": "Wrong target",
                "followType": "Tracking Shot",
                "followSmoothness": 0.8,
                "fieldOfView": 0.8,
            }
        )
        controller.home_translation = [-7.0, 0.0, 0.03]
        controller.home_rotation = [0.0, 0.0, 1.0, 0.0]
        controller.home_viewpoint_position = [-6.935, 0.0, 0.05]
        controller.home_viewpoint_orientation = [0.0, 1.0, 0.0, -0.384]
        controller.home_viewpoint_follow = "Dream Mode Drone"
        controller.home_viewpoint_follow_type = "Mounted Shot"
        controller.home_viewpoint_follow_smoothness = 0.0
        controller.home_viewpoint_field_of_view = 2.0943951
        controller.armed = True
        controller.airborne = True
        controller.ground_contact_since = None
        controller.rate_integral = [1.0, 1.0, 1.0]
        controller.last_motor_speeds = (1.0, 1.0, 1.0, 1.0)
        controller.last_commanded_thrust = 1.0
        controller.last_rate_setpoints_deg_s = (1.0, 1.0, 1.0)
        controller.last_gyro_values = (1.0, 1.0, 1.0)
        controller.armed_input_source = "joystick"
        controller.rearm_inhibit = False
        controller.rearm_source_required = None
        controller.rearm_high_seen = False
        controller.boundary_rearm_pending = False
        controller.boundary_neutral_since = None
        controller.explicit_arm_required = False
        controller.manual_arm_requested = False
        controller.disarm_event_count = 0
        controller.last_disarm_reason_code = 0
        controller.inverted_since = 1.0
        controller.recovery_count = 0

        controller._return_home("recovered", reason_code=4)

        self.assertEqual(
            controller.drone_node.getField("translation").value,
            controller.home_translation,
        )
        self.assertEqual(
            controller.drone_node.getField("rotation").value,
            controller.home_rotation,
        )
        self.assertTrue(controller.drone_node.physics_reset)
        self.assertEqual(controller.rearm_source_required, "joystick")
        self.assertTrue(controller.boundary_rearm_pending)
        self.assertEqual(motor.velocity, 0.0)
        self.assertEqual(
            controller.viewpoint_node.getField("followType").value,
            "Mounted Shot",
        )
        self.assertEqual(
            controller.viewpoint_node.getField("followSmoothness").value,
            0.0,
        )
        self.assertEqual(
            controller.viewpoint_node.getField("position").value,
            controller.home_viewpoint_position,
        )
        self.assertEqual(
            controller.viewpoint_node.getField("orientation").value,
            controller.home_viewpoint_orientation,
        )
        self.assertEqual(
            controller.viewpoint_node.getField("follow").value,
            "Dream Mode Drone",
        )
        self.assertEqual(
            controller.viewpoint_node.getField("fieldOfView").value,
            2.0943951,
        )
        self.assertEqual(controller.recovery_count, 1)


if __name__ == "__main__":
    unittest.main()
