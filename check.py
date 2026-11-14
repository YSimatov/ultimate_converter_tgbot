"""Run without a token: python check.py [--smoke]."""

import argparse
import asyncio
import importlib
import importlib.metadata
import subprocess
import sys
import tempfile
from pathlib import Path

from config import Config
from converter import convert
from database import DEFAULTS


async def smoke(config):
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="convert-check-") as root:
        source = Path(root) / "input.png"
        Image.new("RGBA", (64, 48), (255, 0, 0, 128)).save(source)
        output = await convert(source, "png", "jpg", DEFAULTS, config)
        with Image.open(output) as result:
            assert result.size == (64, 48) and result.mode == "RGB"
        print("PASS PNG -> JPG (isolated worker)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--require-all",
        action="store_true",
        help="Fail if an external converter is missing",
    )
    args = parser.parse_args()
    config = Config()
    print("Python:", sys.version.split()[0])
    failed = False
    for label, executable, flag in (
        ("LibreOffice", config.office, "--version"),
        ("FFmpeg", config.ffmpeg, "-version"),
    ):
        if not executable:
            print(label + ": NOT INSTALLED")
            failed |= args.require_all
            continue
        try:
            version = subprocess.run(
                [executable, flag], capture_output=True, timeout=15, check=True
            )
            print(
                label + ": " + version.stdout.decode(errors="replace").splitlines()[0]
            )
        except (OSError, subprocess.SubprocessError, IndexError):
            print(label + ": EXECUTABLE FAILED")
            failed = True
    for name, package in (
        ("aiogram", "aiogram"),
        ("sqlite3", None),
        ("PIL", "Pillow"),
        ("pdf2docx", "pdf2docx"),
        ("pymupdf", "PyMuPDF"),
        ("olefile", "olefile"),
    ):
        try:
            importlib.import_module(name)
            print(
                name
                + ": OK "
                + (importlib.metadata.version(package) if package else "")
            )
        except (ImportError, OSError) as exc:
            print(name + ": UNAVAILABLE (" + type(exc).__name__ + ")")
            failed = True
    if failed:
        raise SystemExit(1)
    if args.smoke:
        asyncio.run(smoke(config))


if __name__ == "__main__":
    main()
