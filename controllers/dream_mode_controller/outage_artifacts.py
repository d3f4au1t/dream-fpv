"""Deferred artifact encoders for Phase 2 outage evidence.

The flight controller copies sensor buffers at the experimental instant and
writes them only after the run. This keeps PNG compression and disk I/O out of
the real-time control path.
"""

from __future__ import annotations

from array import array
import json
import math
from pathlib import Path
import struct
import zlib


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _write_png(path: Path, width: int, height: int, color_type: int, raw: bytes) -> None:
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    encoded = (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level=6))
        + _chunk(b"IEND", b"")
    )
    with path.open("xb") as output:
        output.write(encoded)


def write_bgra_png(
    path: Path,
    frame: bytes,
    width: int,
    height: int,
) -> None:
    expected = width * height * 4
    if len(frame) != expected:
        raise ValueError(f"BGRA frame has {len(frame)} bytes, expected {expected}")
    rgba = bytearray(frame)
    blue = rgba[0::4]
    rgba[0::4] = rgba[2::4]
    rgba[2::4] = blue
    stride = width * 4
    scanlines = bytearray((stride + 1) * height)
    for row in range(height):
        source_start = row * stride
        destination_start = row * (stride + 1) + 1
        scanlines[destination_start : destination_start + stride] = rgba[
            source_start : source_start + stride
        ]
    _write_png(path, width, height, 6, bytes(scanlines))


def write_depth_bundle(
    directory: Path,
    stem: str,
    values: array,
    metadata: dict,
) -> None:
    width = int(metadata["width"])
    height = int(metadata["height"])
    if values.typecode != "f" or len(values) != width * height:
        raise ValueError("Depth buffer does not match its float32 metadata")
    with (directory / f"{stem}.f32").open("xb") as raw_file:
        values.tofile(raw_file)
    with (directory / f"{stem}.json").open("x", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")

    minimum = float(metadata["min_range_m"])
    maximum = float(metadata["max_range_m"])
    span = maximum - minimum
    scanlines = bytearray((width + 1) * height)
    for row in range(height):
        destination = row * (width + 1) + 1
        source = row * width
        for column in range(width):
            value = float(values[source + column])
            if not math.isfinite(value):
                gray = 0
            else:
                normalized = min(1.0, max(0.0, (value - minimum) / span))
                gray = round(255.0 * (1.0 - normalized))
            scanlines[destination + column] = gray
    _write_png(
        directory / f"{stem}.png",
        width,
        height,
        0,
        bytes(scanlines),
    )
