import json
import math
from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorldLayoutTests(unittest.TestCase):
    ROUTE_NAMES = (
        "start pad marker",
        "gate 1 top",
        "checkpoint 2 crossbar",
        "slalom pylon 1",
        "slalom pylon 2",
        "slalom pylon 3",
        "over-under flight bar",
        "raised gate 3 top",
        "gravity gate target marker",
        "reverse L lower-left route marker",
        "stacked gate middle rail",
        "stacked gate orbit pylon",
        "stacked gate middle rail",
        "finish gate crossbar",
        "finish pad marker",
    )

    @classmethod
    def setUpClass(cls):
        cls.world = (PROJECT_ROOT / "worlds" / "dream_mode_research.wbt").read_text(
            encoding="utf-8"
        )
        cls.project = (
            PROJECT_ROOT / "worlds" / ".dream_mode_research.wbproj"
        ).read_text(encoding="utf-8")
        cls.proto = (PROJECT_ROOT / "protos" / "FpvResearchQuad.proto").read_text(
            encoding="utf-8"
        )
        cls.config = json.loads(
            (PROJECT_ROOT / "config" / "controller.json").read_text(
                encoding="utf-8"
            )
        )

    def solid_section(self, name):
        marker = f'name "{name}"'
        for section in self.world.split("Solid {")[1:]:
            if marker in section:
                return section
        self.fail(f"Missing Solid named {name}")

    def translation(self, name):
        match = re.search(
            r"\btranslation\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)",
            self.solid_section(name),
        )
        self.assertIsNotNone(match, f"Missing translation for {name}")
        return tuple(float(value) for value in match.groups())

    def rotation_angle(self, name):
        match = re.search(
            r"\brotation\s+0\s+0\s+1\s+(-?[\d.]+)",
            self.solid_section(name),
        )
        return 0.0 if match is None else float(match.group(1))

    def test_visible_floor_and_collision_floor_match(self):
        floor_section = self.solid_section("floor")
        sizes = re.findall(
            r"\bsize\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
            floor_section,
        )
        self.assertGreaterEqual(len(sizes), 2)
        self.assertEqual(sizes[0], sizes[1])
        self.assertEqual(tuple(map(float, sizes[0])), (50.0, 30.0, 0.2))

    def test_world_random_seed_is_frozen(self):
        self.assertIn("DEF WORLD_INFO WorldInfo {", self.world)
        self.assertIn("randomSeed 1907", self.world)

    def test_recovery_bounds_are_inside_visible_course(self):
        bounds = self.config["flight_profile"]["recovery_bounds"]
        floor_x, floor_y, _ = self.translation("floor")
        floor_size = (50.0, 30.0)
        floor_limits = (
            (floor_x - floor_size[0] / 2, floor_x + floor_size[0] / 2),
            (floor_y - floor_size[1] / 2, floor_y + floor_size[1] / 2),
        )
        for axis, limits in zip(("x_m", "y_m"), floor_limits):
            self.assertGreaterEqual(bounds[axis][0], limits[0] + 0.49)
            self.assertLessEqual(bounds[axis][1], limits[1] - 0.49)

    def test_arena_has_collision_walls_on_all_four_sides(self):
        expected_positions = {
            "left corridor wall": (10.0, 15.0),
            "right corridor wall": (10.0, -15.0),
            "start boundary wall": (-15.0, 0.0),
            "far boundary wall": (35.0, 0.0),
        }
        for name, expected in expected_positions.items():
            self.assertEqual(self.translation(name)[:2], expected)

    def test_route_has_real_straights_and_turns(self):
        points = [self.translation(name)[:2] for name in self.ROUTE_NAMES]
        segments = [
            math.dist(first, second)
            for first, second in zip(points, points[1:])
        ]
        self.assertGreaterEqual(sum(segments), 80.0)
        self.assertGreaterEqual(sum(length >= 6.0 for length in segments), 6)
        self.assertGreaterEqual(min(segments), 1.9)
        self.assertLessEqual(max(segments), 12.1)

        headings = [
            math.atan2(second[1] - first[1], second[0] - first[0])
            for first, second in zip(points, points[1:])
        ]
        turns = [
            (second - first + math.pi) % (2 * math.pi) - math.pi
            for first, second in zip(headings, headings[1:])
        ]
        substantial = [turn for turn in turns if abs(math.degrees(turn)) >= 35.0]
        self.assertGreaterEqual(len(substantial), 3)
        self.assertTrue(any(turn > 0.0 for turn in substantial))
        self.assertTrue(any(turn < 0.0 for turn in substantial))

    def test_turning_gates_are_rotated_into_their_approaches(self):
        assemblies = (
            (
                "checkpoint 2 crossbar",
                "checkpoint 2 outer post",
                "checkpoint 2 inner post",
            ),
            (
                "over-under flight bar",
                "over-under bar left support",
                "over-under bar right support",
            ),
            (
                "raised gate 3 top",
                "raised gate 3 outer post",
                "raised gate 3 inner post",
            ),
            (
                "reverse L middle rail",
                "reverse L center post",
                "reverse L right post",
            ),
            (
                "stacked gate middle rail",
                "stacked gate left post",
                "stacked gate right post",
            ),
            (
                "finish gate crossbar",
                "finish gate right post",
                "finish gate left post",
            ),
        )
        for crossbar, first_post, second_post in assemblies:
            angle = self.rotation_angle(crossbar)
            self.assertGreater(abs(angle), 0.4)
            self.assertAlmostEqual(self.rotation_angle(first_post), angle)
            self.assertAlmostEqual(self.rotation_angle(second_post), angle)

    def test_gravity_gate_is_a_high_tilted_horizontal_opening(self):
        rail_names = (
            "gravity gate north rail",
            "gravity gate south rail",
            "gravity gate west rail",
            "gravity gate east rail",
        )
        for name in rail_names:
            section = self.solid_section(name)
            self.assertIn("rotation 0 1 0 0.2617993877991494", section)
            self.assertGreater(self.translation(name)[2], 4.0)

        north = self.translation("gravity gate north rail")
        south = self.translation("gravity gate south rail")
        west = self.translation("gravity gate west rail")
        east = self.translation("gravity gate east rail")
        self.assertAlmostEqual(north[0], south[0])
        self.assertAlmostEqual(north[2], south[2])
        self.assertGreater(north[1], south[1])
        self.assertLess(west[0], east[0])
        self.assertGreater(west[2], east[2])

        support_names = (
            "gravity gate northwest support",
            "gravity gate northeast support",
            "gravity gate southeast support",
        )
        self.assertEqual(len(support_names), 3)
        for name in support_names:
            self.assertGreater(self.translation(name)[2], 2.0)

    def test_reverse_l_has_three_openings_and_no_upper_left_cell(self):
        bottom_z = self.translation("reverse L bottom rail")[2]
        middle_z = self.translation("reverse L middle rail")[2]
        top_z = self.translation("reverse L upper-right rail")[2]
        self.assertAlmostEqual(middle_z - bottom_z, top_z - middle_z)
        self.assertNotIn('name "reverse L upper-left rail"', self.world)

        center_xy = self.translation("reverse L center post")[:2]
        right_xy = self.translation("reverse L right post")[:2]
        top_xy = self.translation("reverse L upper-right rail")[:2]
        self.assertAlmostEqual(math.dist(center_xy, top_xy), 0.925, places=3)
        self.assertAlmostEqual(math.dist(right_xy, top_xy), 0.825, places=3)

    def test_double_gate_tower_has_two_equal_openings(self):
        rail_names = (
            "stacked gate bottom rail",
            "stacked gate middle rail",
            "stacked gate top rail",
        )
        rail_heights = [self.translation(name)[2] for name in rail_names]
        self.assertAlmostEqual(
            rail_heights[1] - rail_heights[0],
            rail_heights[2] - rail_heights[1],
        )
        self.assertEqual(self.world.count('name "stacked gate middle rail"'), 1)
        self.assertEqual(
            self.translation("stacked gate left post")[2],
            self.translation("stacked gate right post")[2],
        )
        post_spacing = math.dist(
            self.translation("stacked gate left post")[:2],
            self.translation("stacked gate right post")[:2],
        )
        opening_width = post_spacing - 0.22
        opening_height = rail_heights[1] - rail_heights[0] - 0.22
        self.assertGreater(opening_width, 1.8)
        self.assertAlmostEqual(opening_width, opening_height, delta=0.1)

    def test_every_advanced_gate_member_has_matching_collision_geometry(self):
        members = (
            "gravity gate north rail",
            "gravity gate south rail",
            "gravity gate west rail",
            "gravity gate east rail",
            "gravity gate northwest support",
            "gravity gate northeast support",
            "gravity gate southeast support",
            "reverse L bottom rail",
            "reverse L middle rail",
            "reverse L upper-right rail",
            "reverse L lower-left post",
            "reverse L center post",
            "reverse L right post",
            "stacked gate left post",
            "stacked gate right post",
            "stacked gate bottom rail",
            "stacked gate middle rail",
            "stacked gate top rail",
            "stacked gate orbit pylon",
        )
        for name in members:
            section = self.solid_section(name)
            sizes = re.findall(
                r"\bsize\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)",
                section,
            )
            self.assertEqual(len(sizes), 2, name)
            self.assertEqual(sizes[0], sizes[1], name)
            self.assertIn("boundingObject Box {", section, name)

    def test_route_anchors_stay_inside_recovery_bounds(self):
        bounds = self.config["flight_profile"]["recovery_bounds"]
        for name in self.ROUTE_NAMES:
            x, y, _ = self.translation(name)
            self.assertGreaterEqual(x, bounds["x_m"][0] + 0.5)
            self.assertLessEqual(x, bounds["x_m"][1] - 0.5)
            self.assertGreaterEqual(y, bounds["y_m"][0] + 0.5)
            self.assertLessEqual(y, bounds["y_m"][1] - 0.5)

    def test_main_view_is_rigid_fpv_and_matches_sensor_offset(self):
        self.assertIn('DEF FPV_VIEW Viewpoint {', self.world)
        self.assertIn('followType "Mounted Shot"', self.world)
        self.assertIn("followSmoothness 0", self.world)
        self.assertIn("position -6.935 0 0.05", self.world)
        self.assertIn("near 0.05", self.world)
        self.assertIn("far 0.052", self.world)
        self.assertEqual(self.world.count("translation 0.065 0 0.02"), 3)
        self.assertEqual(self.world.count("rotation 0 1 0 -0.3839724354387525"), 4)

    def test_pilot_view_is_a_camera_backed_physical_display(self):
        display_match = re.search(
            r'Display\s*\{(?P<body>.*?)\n\s*\}\n\s*RangeFinder\s*\{',
            self.world,
            re.DOTALL,
        )
        self.assertIsNotNone(display_match)
        display = display_match.group("body")
        self.assertIn('name "pilot display"', display)
        self.assertIn("translation 0.1122863766 0 0.0391049363", display)
        self.assertIn("rotation 0 1 0 -0.3839724354387525", display)
        self.assertIn("width 480", display)
        self.assertIn("height 270", display)
        self.assertNotIn("Group {", display)
        self.assertIn("appearance PBRAppearance {", display)
        self.assertIn("baseColorMap ImageTexture {", display)
        self.assertIn("roughness 1", display)
        self.assertIn("metalness 0", display)
        self.assertIn("geometry IndexedFaceSet {", display)
        self.assertIn("0 0.142222 0.08", display)
        self.assertIn("0 -0.142222 -0.08", display)
        self.assertNotIn("boundingObject", display)

        # Rendering-device overlays would either bypass the emulator or add a
        # duplicate pane over the physical FPV screen.
        for device in (
            "depth",
            "pilot display",
            "research camera",
        ):
            self.assertRegex(
                self.project,
                rf"renderingDevicePerspectives: Dream Mode Drone:{re.escape(device)};0;",
            )
        self.assertNotRegex(
            self.project,
            r"renderingDevicePerspectives: Dream Mode Drone:pilot analog camera;1;",
        )

    def test_research_and_pilot_cameras_are_separate(self):
        self.assertEqual(self.world.count("Camera {"), 2)
        self.assertNotIn("Camera {", self.proto)
        self.assertIn('name "research camera"', self.world)
        self.assertIn('name "pilot analog camera"', self.world)
        pilot = self.world.split('name "pilot analog camera"', 1)[1].split(
            "Display {", 1
        )[0]
        self.assertIn("exposure 1.2", pilot)
        self.assertIn("bloomThreshold 0.7", pilot)
        self.assertIn("motionBlur 24", pilot)
        self.assertIn("noise 0.055", pilot)
        self.assertIn("radialCoefficients -0.08 0.02", pilot)

    def test_all_course_sections_are_present(self):
        for name in (
            "checkpoint 2 crossbar",
            "slalom pylon 1",
            "slalom pylon 2",
            "slalom pylon 3",
            "over-under flight bar",
            "raised gate 3 top",
            "gravity gate north rail",
            "reverse L upper-right rail",
            "stacked gate middle rail",
            "stacked gate orbit pylon",
            "finish gate crossbar",
        ):
            self.assertIn(f'name "{name}"', self.world)


if __name__ == "__main__":
    unittest.main()
