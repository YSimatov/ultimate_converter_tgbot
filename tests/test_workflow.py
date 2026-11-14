import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.types import Message
from PIL import Image

from bot import Application, LimitedWriter, Upload, instance_lock
from config import Config
from converter import ConversionError, inspect_file
from formats import targets


def message(uid=1):
    result = MagicMock(spec=Message)
    result.chat = SimpleNamespace(type="private")
    result.from_user = SimpleNamespace(id=uid)
    result.answer = AsyncMock()
    result.answer_document = AsyncMock()
    result.edit_text = AsyncMock()
    for name in ("document", "photo", "audio", "video", "voice", "animation"):
        setattr(result, name, None)
    return result


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Application(Config(data=self.root))
        (self.root / "temp").mkdir()
        await self.app.db.init()

    async def asyncTearDown(self):
        for uid in list(self.app.tasks):
            await self.app.cancel(uid)
        self.temp.cleanup()

    async def test_upload_detect_select_send_and_cleanup(self):
        seed = self.root / "seed.png"
        Image.new("RGB", (24, 16), "red").save(seed)
        incoming = message()
        incoming.document = SimpleNamespace(
            file_name="<photo>.docx", file_id="file", file_size=seed.stat().st_size
        )
        status = message()
        incoming.answer.return_value = status

        async def download(file_id, destination, timeout):
            destination.write(seed.read_bytes())

        incoming.bot = SimpleNamespace(download=download)
        await self.app.receive(incoming)
        upload = self.app.uploads[1]
        self.assertEqual(upload.ext, "png")
        self.assertIn("jpg", upload.choices)
        self.assertNotIn("png", upload.choices)
        self.assertNotIn(1, self.app.tasks)
        delivered = []

        async def send(document, **kwargs):
            delivered.append(Path(document.path).read_bytes())

        status.answer_document.side_effect = send
        call = SimpleNamespace(
            message=status,
            from_user=SimpleNamespace(id=1),
            data=f"convert:{upload.nonce}:jpg",
            answer=AsyncMock(),
        )
        await self.app.callback(call)
        await self.app.tasks[1]
        self.assertTrue(delivered[0].startswith(b"\xff\xd8"))
        self.assertEqual((await self.app.db.history(1))[0]["status"], "ok")
        self.assertEqual(list((self.root / "temp").iterdir()), [])
        self.assertEqual(self.app.tasks, {})
        self.assertEqual(self.app.uploads, {})

    async def test_cancel_before_job_starts_clears_reservation(self):
        folder = self.root / "temp/job-test"
        folder.mkdir()
        source = folder / "input.png"
        Image.new("RGB", (10, 10)).save(source)
        upload = Upload(source, "input.png", "png", "nonce", time.monotonic(), ("jpg",))
        self.app.uploads[1] = upload
        self.app.tasks[1] = asyncio.create_task(
            self.app.run_job(message(), 1, upload, "jpg")
        )
        await self.app.cancel(1)
        self.assertNotIn(1, self.app.tasks)
        self.assertNotIn(1, self.app.uploads)
        self.assertFalse(folder.exists())

    async def test_parallel_users_and_cancel_running_job(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def conversion(source, *args):
            started.set()
            await release.wait()
            output = source.with_suffix(".jpg")
            output.write_bytes(b"result")
            return output

        with patch("bot.convert", side_effect=conversion):
            for uid in (1, 2):
                folder = self.root / f"temp/job-{uid}"
                folder.mkdir()
                source = folder / "input.png"
                source.write_bytes(b"source")
                upload = Upload(
                    source, "input.png", "png", str(uid), time.monotonic(), ("jpg",)
                )
                self.app.uploads[uid] = upload
                self.app.tasks[uid] = asyncio.create_task(
                    self.app.run_job(message(uid), uid, upload, "jpg")
                )
            await asyncio.wait_for(started.wait(), 3)
            await self.app.cancel(1)
            self.assertIn(2, self.app.tasks)
            # Menus continue to work while another user's converter is waiting.
            call = SimpleNamespace(
                message=message(3),
                from_user=SimpleNamespace(id=3),
                data="settings",
                answer=AsyncMock(),
            )
            await asyncio.wait_for(self.app.callback(call), 3)
            release.set()
            await self.app.tasks[2]
        self.assertEqual((await self.app.db.history(1))[0]["status"], "cancelled")
        self.assertEqual((await self.app.db.history(2))[0]["status"], "ok")

    async def test_cleanup_refuses_other_directories(self):
        outside = self.root / "keep-me"
        outside.mkdir()
        await self.app.remove_job(outside)
        self.assertTrue(outside.exists())

    def test_bounded_download_and_single_instance(self):
        with LimitedWriter(self.root / "download", 5) as destination:
            destination.write(b"123")
            with self.assertRaises(ConversionError):
                destination.write(b"456")
        lock = self.root / "bot.lock"
        with instance_lock(lock), self.assertRaises(SystemExit), instance_lock(lock):
            self.fail("Second instance must not acquire the lock")
        with instance_lock(lock):
            pass

    async def test_scan_pdf_and_media_tracks_filter_targets(self):
        import pymupdf

        source = self.root / "scan.pdf"
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(source)
        info = await inspect_file(source, "pdf", self.app.config)
        ext = info.pop("ext")
        self.assertEqual(targets(ext, True, True, **info), [])
        self.assertNotIn("mp3", targets("mp4", True, True, has_audio=False))
        self.assertIn("webm", targets("mp4", True, True, has_audio=False))


if __name__ == "__main__":
    unittest.main()
