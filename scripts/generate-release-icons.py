"""Generate original geometric Ella icons using only the Python standard library."""

import argparse
import struct
import zlib
from pathlib import Path


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def icon(size: int) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            u, v = (x + 0.5) / size, (y + 0.5) / size
            if (u - 0.5) ** 2 + (v - 0.5) ** 2 > 0.45 ** 2:
                rgba = (0, 0, 0, 0)
            elif (
                0.29 <= u <= 0.39 and 0.25 <= v <= 0.75
                or 0.29 <= u <= 0.67 and (0.25 <= v <= 0.34 or 0.46 <= v <= 0.55 or 0.66 <= v <= 0.75)
            ):
                rgba = (244, 242, 252, 255)
            else:
                rgba = (82 + int(25 * v), 91 + int(17 * v), 151 + int(18 * v), 255)
            rows.extend(rgba)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def generate(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for size in (32, 128):
        (destination / f"{size}x{size}.png").write_bytes(icon(size))
    png = icon(256)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    (destination / "icon.ico").write_bytes(struct.pack("<HHH", 0, 1, 1) + entry + png)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "apps/desktop/src-tauri/icons")
    generate(parser.parse_args().output_dir)
