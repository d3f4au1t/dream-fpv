import copy
import importlib.util
import json
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_apparatus_under_test",
    PROJECT_ROOT / "scripts" / "verify_apparatus.py",
)
VERIFY_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VERIFY_MODULE)


class VerifyApparatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.apparatus = json.loads(
            (PROJECT_ROOT / "config" / "apparatus_v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.zones = json.loads(
            (PROJECT_ROOT / "config" / "course_zones.json").read_text(
                encoding="utf-8"
            )
        )
        cls.config = json.loads(
            (PROJECT_ROOT / "config" / "controller.json").read_text(
                encoding="utf-8"
            )
        )

    def test_semantic_claims_match_executable_apparatus(self):
        VERIFY_MODULE.verify_semantic_claims(
            copy.deepcopy(self.apparatus),
            copy.deepcopy(self.zones),
            copy.deepcopy(self.config),
        )

    def test_boolean_seed_is_not_accepted_as_an_integer(self):
        apparatus = copy.deepcopy(self.apparatus)
        apparatus["random_seed"] = True
        with self.assertRaises(VERIFY_MODULE.SemanticVerificationError):
            VERIFY_MODULE.verify_semantic_claims(
                apparatus,
                copy.deepcopy(self.zones),
                copy.deepcopy(self.config),
            )

    def test_timing_drift_is_detected(self):
        apparatus = copy.deepcopy(self.apparatus)
        apparatus["timing"]["rgb_period_ms"] = 32
        with self.assertRaises(VERIFY_MODULE.SemanticVerificationError):
            VERIFY_MODULE.verify_semantic_claims(
                apparatus,
                copy.deepcopy(self.zones),
                copy.deepcopy(self.config),
            )


if __name__ == "__main__":
    unittest.main()
