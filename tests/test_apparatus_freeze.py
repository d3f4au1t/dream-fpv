import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from apparatus import (  # noqa: E402
        canonical_sha256,
        classify_course_zone,
        load_and_verify_apparatus,
        validate_course_zones,
    )
finally:
    sys.path.remove(str(CONTROLLER_DIR))


class ApparatusFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller_config = json.loads(
            (PROJECT_ROOT / "config" / "controller.json").read_text(
                encoding="utf-8"
            )
        )
        (
            cls.apparatus,
            cls.course_zones,
            cls.locked_hashes,
            cls.manifest_hash,
        ) = load_and_verify_apparatus(PROJECT_ROOT, cls.controller_config)

    def test_frozen_identity_and_seed_are_locked(self):
        self.assertEqual(self.apparatus["apparatus_id"], "dream_fpv_webots")
        self.assertEqual(self.apparatus["version"], "2.3.0")
        self.assertEqual(self.apparatus["status"], "frozen")
        self.assertEqual(self.apparatus["random_seed"], 1907)
        self.assertEqual(len(self.manifest_hash), 64)

    def test_phase_one_manifest_remains_as_a_historical_record(self):
        phase_one = json.loads(
            (PROJECT_ROOT / "config" / "apparatus_v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(phase_one["version"], "1.0.0")
        self.assertEqual(phase_one["status"], "frozen")
        self.assertEqual(
            self.apparatus["inherits"]["git_tag"],
            "apparatus-v1.0.0",
        )

    def test_every_authoritative_file_matches_its_frozen_hash(self):
        self.assertGreaterEqual(len(self.locked_hashes), 8)
        self.assertEqual(
            self.locked_hashes,
            self.apparatus["locked_files"],
        )

    def test_world_uses_the_frozen_seed_and_time_step(self):
        world = (PROJECT_ROOT / "worlds" / "dream_mode_research.wbt").read_text(
            encoding="utf-8"
        )
        self.assertIn("DEF WORLD_INFO WorldInfo {", world)
        self.assertIn("randomSeed 1907", world)
        self.assertIn("basicTimeStep 8", world)

    def test_course_zones_are_valid_inside_recovery_volume(self):
        validate_course_zones(
            self.course_zones,
            self.controller_config["flight_profile"]["recovery_bounds"],
        )
        self.assertEqual(len(self.course_zones["zones"]), 10)
        self.assertEqual(
            [zone["code"] for zone in self.course_zones["zones"]],
            list(range(1, 11)),
        )

    def test_representative_course_anchors_have_stable_zone_ids(self):
        anchors = {
            1: (-7.0, 0.0, 0.03),
            2: (1.8, -2.0, 1.0),
            3: (3.0, -5.0, 1.35),
            4: (9.0, -0.5, 1.2),
            5: (12.0, 6.0, 1.35),
            6: (18.0, 2.0, 1.765),
            7: (25.0, 6.0, 4.5),
            8: (28.271955992454, -0.485362671697, 0.925),
            9: (20.0, -9.0, 3.275),
            10: (14.0, -6.0, 1.35),
        }
        for expected_code, position in anchors.items():
            code, zone_id = classify_course_zone(position, self.course_zones)
            self.assertEqual(code, expected_code, zone_id)

    def test_open_transfers_and_nonfinite_positions_use_fallback(self):
        self.assertEqual(
            classify_course_zone((14.0, 0.0, 1.0), self.course_zones),
            (0, "transfer"),
        )
        self.assertEqual(
            classify_course_zone((float("nan"), 0.0, 1.0), self.course_zones),
            (0, "transfer"),
        )

    def test_zone_boundaries_are_lower_inclusive_upper_exclusive(self):
        self.assertEqual(
            classify_course_zone((-8.0, 0.0, 1.0), self.course_zones)[0],
            1,
        )
        self.assertEqual(
            classify_course_zone((0.5, 0.0, 1.0), self.course_zones)[0],
            0,
        )

    def test_canonical_hash_ignores_json_key_order_but_not_values(self):
        first = {"a": 1, "b": {"x": 2, "y": 3}}
        reordered = {"b": {"y": 3, "x": 2}, "a": 1}
        changed = {"a": 1, "b": {"x": 2, "y": 4}}
        self.assertEqual(canonical_sha256(first), canonical_sha256(reordered))
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(changed))


if __name__ == "__main__":
    unittest.main()
