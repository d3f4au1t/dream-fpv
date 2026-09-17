from array import array
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from outage_artifacts import write_bgra_png, write_depth_bundle  # noqa: E402
finally:
    sys.path.remove(str(CONTROLLER_DIR))


def png_header(path: Path):
    data = path.read_bytes()[:29]
    return struct.unpack(">IIBBBBB", data[16:29])


class OutageArtifactTests(unittest.TestCase):
    def test_bgra_encoder_writes_an_rgba_png(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frame.png"
            write_bgra_png(
                path,
                bytes((30, 20, 10, 255, 60, 50, 40, 255)),
                2,
                1,
            )
            self.assertEqual(png_header(path), (2, 1, 8, 6, 0, 0, 0))

    def test_depth_bundle_writes_raw_preview_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = {
                "width": 2,
                "height": 2,
                "min_range_m": 0.05,
                "max_range_m": 20.0,
            }
            write_depth_bundle(
                directory,
                "depth",
                array("f", (0.05, 1.0, 10.0, 20.0)),
                metadata,
            )
            self.assertEqual((directory / "depth.f32").stat().st_size, 16)
            self.assertEqual(
                json.loads((directory / "depth.json").read_text()),
                metadata,
            )
            self.assertEqual(png_header(directory / "depth.png"), (2, 2, 8, 0, 0, 0, 0))

    def test_rejects_incorrect_buffer_sizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                write_bgra_png(Path(temporary) / "bad.png", b"\0" * 4, 2, 2)


if __name__ == "__main__":
    unittest.main()
