"""Inspect untrusted input inside the killable worker, before offering formats."""

import re
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

from formats import GROUPS, group


class InvalidFile(ValueError):
    """A safe, user-facing explanation of an invalid input."""


def inspect_file(path: Path, hint: str, ffmpeg: str | None) -> dict:
    with path.open("rb") as stream:
        header = stream.read(4096)
    if not header:
        raise InvalidFile("Файл пуст. Отправьте другой файл.")
    info = {"has_audio": True, "has_video": True, "pdf_text": True}
    ext = hint
    if header.startswith(b"%PDF-"):
        import pymupdf

        ext = "pdf"
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise InvalidFile("PDF защищён паролем. Сначала снимите защиту.")
            info["pdf_text"] = any(page.get_text().strip() for page in doc)
    elif header.startswith(b"PK"):
        with zipfile.ZipFile(path) as archive:
            items = archive.infolist()
            if len(items) > 10000 or sum(x.file_size for x in items) > 256 * 1024**2:
                raise InvalidFile("Слишком большой распакованный документ.")
            names = set(archive.namelist())
            if "mimetype" in names:
                mime_item = archive.getinfo("mimetype")
                if mime_item.file_size > 200:
                    raise InvalidFile("Некорректный OpenDocument.")
                mime = archive.read("mimetype").decode("ascii")
                ext = {
                    "application/vnd.oasis.opendocument.text": "odt",
                    "application/vnd.oasis.opendocument.spreadsheet": "ods",
                    "application/vnd.oasis.opendocument.presentation": "odp",
                }.get(mime, "")
            else:
                ext = next(
                    (
                        kind
                        for kind, entry in (
                            ("docx", "word/document.xml"),
                            ("xlsx", "xl/workbook.xml"),
                            ("pptx", "ppt/presentation.xml"),
                        )
                        if entry in names
                    ),
                    "",
                )
            if not ext:
                raise InvalidFile(
                    "Этот ZIP-архив не является поддерживаемым документом."
                )
    elif header.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        # Compound Office containers have no unique magic per application.
        import olefile

        with olefile.OleFileIO(path) as document:
            if document.exists("EncryptedPackage"):
                raise InvalidFile("Документ зашифрован. Сначала снимите пароль.")
            ext = next(
                (
                    kind
                    for kind, stream in (
                        ("doc", "WordDocument"),
                        ("xls", "Workbook"),
                        ("xls", "Book"),
                        ("ppt", "PowerPoint Document"),
                    )
                    if document.exists(stream)
                ),
                "",
            )
        if not ext:
            raise InvalidFile("Неизвестный бинарный документ Office.")
    elif header.startswith(b"{\\rtf"):
        ext = "rtf"
    else:
        from PIL import Image, UnidentifiedImageError

        Image.MAX_IMAGE_PIXELS = 40_000_000
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                with Image.open(path) as image:
                    ext = {
                        "JPEG": "jpg",
                        "PNG": "png",
                        "WEBP": "webp",
                        "BMP": "bmp",
                        "TIFF": "tiff",
                    }.get(image.format, "")
                    image.verify()
                    if not ext:
                        raise InvalidFile("Этот формат изображения не поддерживается.")
                return {"ext": ext, **info}
            except UnidentifiedImageError:
                pass
        media_signature = (
            header.startswith((b"ID3", b"fLaC", b"OggS", b"\x1aE\xdf\xa3"))
            or header.startswith(b"RIFF")
            and header[8:12] in (b"WAVE", b"AVI ")
            or header[4:8] == b"ftyp"
            or len(header) > 1
            and header[0] == 0xFF
            and header[1] & 0xE0 == 0xE0
        )
        if media_signature or group(hint) in ("Аудио", "Видео"):
            if not ffmpeg:
                raise InvalidFile(
                    "Аудио и видео временно недоступны. Сообщите владельцу бота."
                )
            result = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                ],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32"
                else 0,
            )
            summary = result.stderr.decode("utf-8", errors="replace")
            info["has_audio"] = bool(
                re.search(r"Stream #\d+:\d+[^\n]*: Audio:", summary)
            )
            video_streams = re.findall(r"Stream #\d+:\d+[^\n]*: Video:[^\n]*", summary)
            info["has_video"] = any(
                "(attached pic)" not in stream for stream in video_streams
            )
            if not info["has_audio"] and not info["has_video"]:
                raise InvalidFile(
                    "Не удалось прочитать аудио или видео. Файл повреждён или кодек недоступен."
                )
            detected = re.search(r"Input #0, (.+?), from ", summary)
            container = detected.group(1).split(",") if detected else []
            mapping = {
                "mp3": "mp3",
                "wav": "wav",
                "ogg": "ogg",
                "flac": "flac",
                "avi": "avi",
                "matroska": "mkv",
                "webm": "webm",
                "mov": "mov",
                "mp4": "mp4",
                "m4a": "m4a",
            }
            candidates = [mapping[x] for x in container if x in mapping]
            if "matroska" in container:
                ext = "webm" if b"webm" in header[:128] else "mkv"
            elif "mov" in container and header[4:8] == b"ftyp":
                ext = (
                    "mov"
                    if header[8:12] == b"qt  "
                    else "mp4"
                    if info["has_video"]
                    else "m4a"
                )
            elif hint in candidates:
                ext = hint
            elif candidates:
                ext = candidates[0]
            else:
                raise InvalidFile("Контейнер этого медиафайла не поддерживается.")
            if not info["has_video"] and ext in GROUPS["Видео"]:
                # Audio-only MPEG-4 is represented as M4A, Matroska audio is unsupported.
                if ext in ("mp4", "mov"):
                    ext = "m4a"
                else:
                    raise InvalidFile("В этом видеоконтейнере нет видеодорожки.")
        elif hint in ("txt", "csv", "html"):
            try:
                text = path.read_text(encoding="utf-8-sig")
            except UnicodeError as exc:
                raise InvalidFile(
                    "Сохраните текстовый файл в кодировке UTF-8."
                ) from exc
            if "\x00" in text:
                raise InvalidFile("Содержимое не похоже на текстовый файл.")
            if re.search(r"(?is)^\s*(?:<!doctype\s+html\b|<html\b)", text):
                ext = "html"
        else:
            raise InvalidFile("Содержимое не соответствует поддерживаемым форматам.")
    return {"ext": ext, **info}
