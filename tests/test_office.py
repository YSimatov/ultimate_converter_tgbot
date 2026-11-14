"""Real Office matrix. Install requirements-dev.txt and LibreOffice to run."""

import asyncio
import importlib.util
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from config import Config
from converter import convert, inspect_file
from database import DEFAULTS
from formats import GROUPS, targets

HAS_FIXTURES = all(
    importlib.util.find_spec(name) for name in ("docx", "openpyxl", "pptx")
)


@unittest.skipUnless(HAS_FIXTURES, "Install requirements-dev.txt for Office fixtures")
class OfficeMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_office_pairs(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation

        config = Config()
        if not config.office:
            self.skipTest("LibreOffice is not installed")
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            seeds = {}
            document = Document()
            document.add_paragraph("Hello converter / Проверка UTF-8")
            seeds["Документы"] = root / "seed.docx"
            document.save(seeds["Документы"])
            workbook = Workbook()
            workbook.active.append(["Hello converter", "Проверка UTF-8", 42])
            seeds["Таблицы"] = root / "seed.xlsx"
            workbook.save(seeds["Таблицы"])
            presentation = Presentation()
            slide = presentation.slides.add_slide(presentation.slide_layouts[0])
            slide.shapes.title.text = "Hello converter"
            seeds["Презентации"] = root / "seed.pptx"
            presentation.save(seeds["Презентации"])
            limit = asyncio.Semaphore(2)

            async def fixture(seed, source_ext):
                source_dir = root / ("fixture-" + source_ext)
                source_dir.mkdir()
                source = source_dir / ("input." + seed.suffix.lstrip("."))
                shutil.copyfile(seed, source)
                if source_ext != seed.suffix.lstrip("."):
                    async with limit:
                        source = await convert(
                            source,
                            seed.suffix.lstrip("."),
                            source_ext,
                            DEFAULTS,
                            config,
                        )
                    if source_ext == "html":
                        with zipfile.ZipFile(source) as archive:
                            archive.extractall(source_dir / "html")
                        source = next((source_dir / "html").glob("*.html"))
                return source_ext, source

            fixtures = await asyncio.gather(
                *(
                    fixture(seed, ext)
                    for category, seed in seeds.items()
                    for ext in GROUPS[category]
                    if ext != "pdf"
                )
            )

            async def verify(source_ext, source, target_ext):
                async with limit:
                    pair = f"{source_ext} → {target_ext}"
                    job_dir = root / f"{source_ext}-to-{target_ext}"
                    job_dir.mkdir()
                    job_source = job_dir / ("input." + source_ext)
                    shutil.copyfile(source, job_source)
                    output = await convert(
                        job_source, source_ext, target_ext, DEFAULTS, config
                    )
                    self.assertGreater(output.stat().st_size, 10, pair)
                    if target_ext == "html":
                        with zipfile.ZipFile(output) as archive:
                            self.assertTrue(
                                any(x.endswith(".html") for x in archive.namelist()),
                                pair,
                            )
                    else:
                        info = await inspect_file(output, target_ext, config)
                        self.assertEqual(info["ext"], target_ext, pair)
                    if target_ext in ("txt", "csv"):
                        text = output.read_text(encoding="utf-8-sig")
                        self.assertIn("Hello converter", text, pair)
                        self.assertIn("Проверка UTF-8", text, pair)

            pairs = [
                (ext, source, target)
                for ext, source in fixtures
                for target in targets(ext, True, False)
            ]
            outcomes = await asyncio.gather(
                *(verify(*pair) for pair in pairs), return_exceptions=True
            )
            errors = [
                f"{pair[0]} → {pair[2]}: {result!r}"
                for pair, result in zip(pairs, outcomes, strict=True)
                if isinstance(result, BaseException)
            ]
            self.assertFalse(errors, "\n".join(errors))
            self.assertEqual(len(outcomes), 61)


if __name__ == "__main__":
    unittest.main()
