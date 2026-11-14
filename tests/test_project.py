import asyncio
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import UTC

from formats import GROUPS, normalize, targets


class FormatTests(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(targets("pdf", False, False), ["docx", "txt"])
        self.assertEqual(targets("docx", False, True), [])
        self.assertNotIn("pptx", targets("pdf", True, True))
        self.assertIn("pdf", targets("pptx", True, False))
        self.assertNotIn("mp4", targets("mp3", True, True))
        self.assertIn("mp3", targets("mp4", False, True))
        for values in GROUPS.values():
            for ext in values:
                self.assertNotIn(ext, targets(ext, True, True))
        self.assertEqual(normalize(".JPEG"), "jpg")


HAS_DB = True
HAS_ENV = importlib.util.find_spec("dotenv") is not None


@unittest.skipUnless(HAS_DB, "aiosqlite is not installed")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolation_history_and_validation(self):
        from database import Database

        with tempfile.TemporaryDirectory() as root:
            db = Database(Path(root) / "test.sqlite3")
            await db.init()
            await db.set(1, "image", "70")
            self.assertEqual((await db.settings(1))["image"], "70")
            self.assertEqual((await db.settings(2))["image"], "90")
            with self.assertRaises(ValueError):
                await db.set(1, "user_id", "2")
            await db.record(1, "<test>.png", "png", "jpg", "ok")
            self.assertEqual(len(await db.history(1)), 1)
            self.assertEqual(await db.history(2), [])
            await db.clear(1)
            self.assertEqual(await db.history(1), [])


@unittest.skipUnless(
    importlib.util.find_spec("aiogram") and HAS_ENV, "aiogram is not installed"
)
class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_sections_and_quality_callback(self):
        from datetime import datetime
        from unittest.mock import AsyncMock

        from aiogram.types import Chat, Message, User

        from bot import Application
        from config import Config
        from database import Database

        with tempfile.TemporaryDirectory() as root:
            app = Application(Config())
            app.db = Database(Path(root) / "test.sqlite3")
            await app.db.init()
            app.show = AsyncMock()
            call = SimpleNamespace(
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=123, type="private"),
                ),
                from_user=User(id=123, is_bot=False, first_name="Test"),
                answer=AsyncMock(),
            )
            for section in (
                "home",
                "upload",
                "settings",
                "formats",
                "help",
                "history",
                "clear_history",
                "cancel",
            ):
                call.data = section
                await app.callback(call)
                self.assertTrue(app.show.await_args.args[1])
            call.data = "set:audio:320"
            await app.callback(call)
            self.assertEqual((await app.db.settings(123))["audio"], "320")
            self.assertEqual((await app.db.settings(456))["audio"], "192")


@unittest.skipUnless(HAS_ENV, "python-dotenv is not installed")
class ConversionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from config import Config

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config()
        self.settings = {"image": "90", "audio": "192", "video": "23"}

    def tearDown(self):
        self.temp.cleanup()

    async def test_real_image_and_corrupt_input(self):
        from PIL import Image

        from converter import ConversionError, convert

        source = self.root / "input.png"
        Image.new("RGBA", (30, 20), (0, 0, 255, 0)).save(source)
        out = await convert(source, "png", "jpg", self.settings, self.config)
        with Image.open(out) as img:
            self.assertEqual(img.size, (30, 20))
            self.assertGreater(img.getpixel((0, 0))[0], 240)
        # Each job uses a fresh directory, just as the bot does.
        broken = self.root / "bad"
        broken.mkdir()
        source = broken / "input.png"
        source.write_bytes(b"not an image")
        with self.assertRaises(ConversionError):
            await convert(source, "png", "jpg", self.settings, self.config)

    async def test_reject_impossible_pair(self):
        from converter import ConversionError, convert

        with self.assertRaises(ConversionError):
            await convert(
                self.root / "input.mp3", "mp3", "docx", self.settings, self.config
            )

    async def test_timeout_and_cancellation_stop_process(self):
        from unittest.mock import patch

        from converter import ConversionError, convert

        source = self.root / "input.png"
        source.write_bytes(b"x")
        real_create = asyncio.create_subprocess_exec
        processes = []

        async def sleeper(*args, **kwargs):
            if len(args) > 1 and str(args[1]).endswith("worker.py"):
                process = await real_create(
                    sys.executable, "-c", "import time; time.sleep(30)", **kwargs
                )
                processes.append(process)
                return process
            return await real_create(*args, **kwargs)

        self.config.timeout = 0.3
        with patch("converter.asyncio.create_subprocess_exec", side_effect=sleeper):
            with self.assertRaises(ConversionError):
                await convert(source, "png", "jpg", self.settings, self.config)
            self.assertIsNotNone(processes[-1].returncode)
            self.config.timeout = 30
            task = asyncio.create_task(
                convert(source, "png", "jpg", self.settings, self.config)
            )
            while len(processes) < 2:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(processes[-1].returncode)

    async def test_cancel_during_process_creation(self):
        from unittest.mock import patch

        from converter import convert

        source = self.root / "input.png"
        source.write_bytes(b"x")
        real_create = asyncio.create_subprocess_exec
        spawning = asyncio.Event()
        spawned = []

        async def delayed_spawn(*args, **kwargs):
            if len(args) > 1 and str(args[1]).endswith("worker.py"):
                spawning.set()
                await asyncio.sleep(0.1)
                proc = await real_create(
                    sys.executable, "-c", "import time; time.sleep(30)", **kwargs
                )
                spawned.append(proc)
                return proc
            return await real_create(*args, **kwargs)

        with patch(
            "converter.asyncio.create_subprocess_exec", side_effect=delayed_spawn
        ):
            task = asyncio.create_task(
                convert(source, "png", "jpg", self.settings, self.config)
            )
            await spawning.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(spawned[0].returncode)

    async def test_output_size_limit(self):
        from PIL import Image

        from converter import ConversionError, convert

        source = self.root / "input.png"
        Image.new("RGB", (100, 100)).save(source)
        self.config.max_output = 100
        with self.assertRaises(ConversionError):
            await convert(source, "png", "bmp", self.settings, self.config)

    @unittest.skipUnless(
        importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed"
    )
    async def test_pdf_text(self):
        import pymupdf

        from converter import convert

        for target in ("txt",):
            folder = self.root / target
            folder.mkdir()
            source = folder / "input.pdf"
            with pymupdf.open() as doc:
                doc.new_page().insert_text((72, 72), "Hello converter")
                doc.save(source)
            out = await convert(source, "pdf", target, self.settings, self.config)
            self.assertGreater(out.stat().st_size, 10)
            if target == "txt":
                self.assertIn("Hello converter", out.read_text(encoding="utf-8"))

    async def test_pdf_docx(self):
        if not all(
            importlib.util.find_spec(name)
            for name in ("pymupdf", "pdf2docx", "fontTools", "cv2")
        ):
            self.skipTest("PDF-to-DOCX dependencies are not installed")
        import pymupdf
        from docx import Document

        from converter import convert

        source = self.root / "input.pdf"
        with pymupdf.open() as doc:
            doc.new_page().insert_text((72, 72), "Hello converter")
            doc.save(source)
        out = await convert(source, "pdf", "docx", self.settings, self.config)
        self.assertIn(
            "Hello converter", "\n".join(p.text for p in Document(out).paragraphs)
        )

    async def test_ffmpeg_roundtrip(self):
        if not self.config.ffmpeg:
            self.skipTest("FFmpeg is not installed")
        import wave

        from converter import convert

        source = self.root / "input.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        out = await convert(source, "wav", "mp3", self.settings, self.config)
        self.assertGreater(out.stat().st_size, 100)

    async def test_image_formats(self):
        from PIL import Image

        from converter import convert

        for ext in GROUPS["Изображения"]:
            folder = self.root / ext
            folder.mkdir()
            source = folder / "input.png"
            Image.new("RGB", (32, 24), "red").save(source)
            if ext == "png":
                continue
            out = await convert(source, "png", ext, self.settings, self.config)
            with Image.open(out) as result:
                self.assertEqual(result.size, (32, 24))

    async def test_video_and_audio_formats(self):
        if not self.config.ffmpeg:
            self.skipTest("FFmpeg is not installed")
        from converter import convert

        seed = self.root / "seed.mp4"
        result = await asyncio.create_subprocess_exec(
            self.config.ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=10:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(seed),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await result.communicate()
        self.assertEqual(result.returncode, 0, error.decode(errors="replace"))
        for ext in targets("mp4", False, True):
            folder = self.root / ext
            folder.mkdir()
            source = folder / "input.mp4"
            shutil.copyfile(seed, source)
            output = await convert(source, "mp4", ext, self.settings, self.config)
            self.assertGreater(output.stat().st_size, 100)
            # Verify that the exported file can actually be decoded.
            probe = await asyncio.create_subprocess_exec(
                self.config.ffmpeg,
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(output),
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, error = await probe.communicate()
            self.assertEqual(probe.returncode, 0, error.decode(errors="replace"))

    async def test_missing_audio_track(self):
        if not self.config.ffmpeg:
            self.skipTest("FFmpeg is not installed")
        from converter import ConversionError, convert

        source = self.root / "silent.mp4"
        process = await asyncio.create_subprocess_exec(
            self.config.ffmpeg,
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x48:d=0.1",
            "-c:v",
            "libx264",
            str(source),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        with self.assertRaises(ConversionError):
            await convert(source, "mp4", "mp3", self.settings, self.config)

    async def test_libreoffice(self):
        if not self.config.office:
            self.skipTest("LibreOffice is not installed")
        from converter import convert

        source = self.root / "input.txt"
        source.write_text("Hello converter", encoding="utf-8")
        out = await convert(source, "txt", "docx", self.settings, self.config)
        self.assertGreater(out.stat().st_size, 100)

    async def test_html_is_standard_odt(self):
        if not self.config.office:
            self.skipTest("LibreOffice is not installed")
        from converter import convert, inspect_file

        source = self.root / "input.html"
        source.write_text(
            '<!doctype html><html><meta charset="utf-8"><body>Hello converter</body></html>',
            encoding="utf-8",
        )
        output = await convert(source, "html", "odt", self.settings, self.config)
        self.assertEqual((await inspect_file(output, "odt", self.config))["ext"], "odt")


if __name__ == "__main__":
    unittest.main()
