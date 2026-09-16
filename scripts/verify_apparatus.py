#!/usr/bin/env python3
"""Verify the frozen Dream FPV apparatus without opening Webots."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_DIR = PROJECT_ROOT / "controllers" / "dream_mode_controller"
sys.path.insert(0, str(CONTROLLER_DIR))
try:
    from apparatus import ApparatusIntegrityError, load_and_verify_apparatus
finally:
    sys.path.remove(str(CONTROLLER_DIR))


def main() -> int:
    controller_config = json.loads(
        (PROJECT_ROOT / "config" / "controller.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        apparatus, zones, locked_hashes, manifest_hash = load_and_verify_apparatus(
            PROJECT_ROOT,
            controller_config,
        )
    except (ApparatusIntegrityError, OSError, ValueError) as error:
        print(f"Apparatus verification failed: {error}", file=sys.stderr)
        return 1

    print(
        f"Verified {apparatus['apparatus_id']} v{apparatus['version']} "
        f"({len(locked_hashes)} locked files, {len(zones['zones'])} zones)."
    )
    print(f"Manifest SHA-256: {manifest_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
