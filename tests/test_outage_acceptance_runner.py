import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "outage_baseline_test_under_test"
SPEC = importlib.util.spec_from_file_location(
    MODULE_NAME,
    PROJECT_ROOT / "scripts" / "outage_baseline_test.py",
)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[MODULE_NAME] = RUNNER
SPEC.loader.exec_module(RUNNER)


class OutageAcceptanceRunnerTests(unittest.TestCase):
    def test_requested_analog_style_survives_environment_cleanup(self):
        with patch.dict(
            os.environ,
            {
                "DREAM_MODE_VIDEO_STYLE": "analog",
                "DREAM_MODE_OUTAGE_MODE": "randomized",
            },
            clear=True,
        ):
            environment = RUNNER.sanitized_launch_environment()

        self.assertEqual(environment["DREAM_MODE_VIDEO_STYLE"], "analog")
        self.assertNotIn("DREAM_MODE_OUTAGE_MODE", environment)

    def test_invalid_video_style_is_rejected_before_launch(self):
        with patch.dict(
            os.environ,
            {"DREAM_MODE_VIDEO_STYLE": "broadcast"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                RUNNER.BaselineValidationError,
                "DREAM_MODE_VIDEO_STYLE",
            ):
                RUNNER.sanitized_launch_environment()


if __name__ == "__main__":
    unittest.main()
