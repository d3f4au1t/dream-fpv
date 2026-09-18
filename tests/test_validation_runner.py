import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "flight_dynamics_test_under_test"
SPEC = importlib.util.spec_from_file_location(
    MODULE_NAME,
    PROJECT_ROOT / "scripts" / "flight_dynamics_test.py",
)
VALIDATION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[MODULE_NAME] = VALIDATION
SPEC.loader.exec_module(VALIDATION)


def minimal_scenario(name="retry_test"):
    return VALIDATION.Scenario(
        name=name,
        sequence=[[0.0, 0.0, 0.0, 0.0, 0.0]],
        duration_s=0.1,
        kind="throttle",
        throttle=0.0,
    )


class ValidationRunnerTests(unittest.TestCase):
    def test_acceptance_suite_has_72_scenarios(self):
        self.assertEqual(len(VALIDATION.build_scenarios()), 72)

    def test_single_session_queue_preserves_scenario_order_and_isolation(self):
        scenarios = [minimal_scenario("first"), minimal_scenario("second")]
        run_root = Path("/tmp/dream-mode-single-session")
        queue = VALIDATION.build_single_session_queue(scenarios, run_root)

        self.assertEqual(queue["next_index"], 0)
        self.assertEqual(queue["completed"], [])
        self.assertEqual(
            [scenario["name"] for scenario in queue["scenarios"]],
            ["first", "second"],
        )
        self.assertEqual(
            queue["scenarios"][1]["environment"]["DREAM_MODE_LOG_DIR"],
            str(run_root / "second"),
        )
        self.assertEqual(
            queue["scenarios"][0]["environment"]["DREAM_MODE_SMOKE_STEPS"],
            "13",
        )

    def test_retryable_startup_failure_uses_fresh_directories(self):
        scenario = minimal_scenario()
        sentinel = object()
        calls = []

        def fake_run(_scenario, log_dir):
            calls.append(log_dir.name)
            if len(calls) < 3:
                raise VALIDATION.WebotsStartupError("transient startup")
            return sentinel

        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(VALIDATION, "_run_scenario_once", side_effect=fake_run):
                with patch.object(VALIDATION.time, "sleep"):
                    result = VALIDATION.run_scenario(
                        scenario,
                        0,
                        Path(temporary_directory),
                    )

        self.assertIs(result, sentinel)
        self.assertEqual(
            calls,
            ["retry_test", "retry_test__retry_2", "retry_test__retry_3"],
        )

    def test_dynamics_failure_is_not_hidden_by_startup_retry(self):
        scenario = minimal_scenario()
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch.object(
                VALIDATION,
                "_run_scenario_once",
                side_effect=RuntimeError("real dynamics failure"),
            ) as run_once:
                with self.assertRaisesRegex(RuntimeError, "real dynamics failure"):
                    VALIDATION.run_scenario(
                        scenario,
                        0,
                        Path(temporary_directory),
                    )
        run_once.assert_called_once()

    def test_replay_fingerprint_mismatch_is_reported(self):
        first = VALIDATION.Result(
            scenario=minimal_scenario("determinism_replay_a"),
            rows=[],
            log_dir=Path("a"),
            run_manifest={"run_fingerprint_sha256": "a" * 64},
        )
        second = VALIDATION.Result(
            scenario=minimal_scenario("determinism_replay_b"),
            rows=[],
            log_dir=Path("b"),
            run_manifest={"run_fingerprint_sha256": "b" * 64},
        )
        failures = []
        VALIDATION.check_replay_fingerprints([first, second], failures)
        self.assertEqual(failures, ["determinism: replay run fingerprints differ"])


if __name__ == "__main__":
    unittest.main()
