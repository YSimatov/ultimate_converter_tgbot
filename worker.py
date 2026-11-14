"""One isolated conversion. Invoked by converter.py, never by a Telegram handler."""

import json
import subprocess
import sys
import warnings
import zipfile
from pathlib import Path

from formats import GROUPS, group
from inspection import InvalidFile, inspect_file

OFFICE_FILTERS = {
    "Документы": {
        "pdf": "writer_pdf_Export",
        "docx": "Office Open XML Text",
        "doc": "MS Word 97",
        "txt": "Text (encoded):UTF8",
        "rtf": "Rich Text Format",
        "odt": "writer8",
        "html": "HTML (StarWriter)",
    },
    "Таблицы": {
        "pdf": "calc_pdf_Export",
        "xlsx": "Calc MS Excel 2007 XML",
        "xls": "MS Excel 97",
        "csv": "Text - txt - csv (StarCalc):44,34,76",
        "ods": "calc8",
    },
    "Презентации": {
        "pdf": "impress_pdf_Export",
        "pptx": "Impress MS PowerPoint 2007 XML",
        "ppt": "MS PowerPoint 97",
        "odp": "impress8",
    },
}


def validate(path: Path, ext: str):
    with path.open("rb") as stream:
        data = stream.read(16)
    if ext in ("docx", "xlsx", "pptx", "odt", "ods", "odp"):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(x.file_size for x in infos) > 256 * 1024**2:
                raise ValueError("Слишком большой распакованный документ.")
            prefix = {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}
            if ext in prefix and not any(
                x.filename.startswith(prefix[ext]) for x in infos
            ):
                raise ValueError("Содержимое не соответствует расширению файла.")
            if ext.startswith("od"):
                expected = {"odt": "text", "ods": "spreadsheet", "odp": "presentation"}[
                    ext
                ]
                if (
                    archive.read("mimetype").decode()
                    != f"application/vnd.oasis.opendocument.{expected}"
                ):
                    raise ValueError("Неверный тип OpenDocument.")
    if ext in ("doc", "xls", "ppt") and not data.startswith(
        bytes.fromhex("D0CF11E0A1B11AE1")
    ):
        raise ValueError("Ожидается настоящий бинарный документ Office.")
    if ext == "pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("Файл не является PDF.")
    if ext == "rtf" and not data.startswith(b"{\\rtf"):
        raise ValueError("Файл не является RTF.")
    if ext in ("txt", "csv", "html") and b"\x00" in data:
        raise ValueError("Ожидается текстовый файл UTF-8.")


def image(source, target, ext, quality):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 40_000_000
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(source) as original:
        # Use the first frame consistently for all supported output formats.
        original.seek(0)
        frame = ImageOps.exif_transpose(original)
        if ext in ("jpg", "bmp"):
            rgba = frame.convert("RGBA")
            frame = Image.new("RGB", rgba.size, "white")
            frame.paste(rgba, mask=rgba.getchannel("A"))
        elif frame.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            frame = frame.convert("RGBA")
        options = {"quality": int(quality)} if ext in ("jpg", "webp") else {}
        if ext == "png":
            options["compress_level"] = 6
        if ext == "tiff":
            options["compression"] = "tiff_deflate"
        frame.save(
            target,
            format={"jpg": "JPEG", "tiff": "TIFF"}.get(ext, ext.upper()),
            **options,
        )


def pdf(source, target, ext):
    import pymupdf

    with pymupdf.open(source) as doc:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем. Сначала снимите защиту.")
        if not doc.page_count:
            raise ValueError("PDF не содержит страниц.")
        if not any(page.get_text().strip() for page in doc):
            raise ValueError(
                "В PDF нет текстового слоя. Для скана требуется OCR, он не поддерживается."
            )
        if ext == "txt":
            target.write_text(
                "\n\n".join(page.get_text() for page in doc), encoding="utf-8"
            )
            return
    from pdf2docx import Converter

    converter = Converter(str(source))
    try:
        converter.convert(str(target), multi_processing=False)
    finally:
        converter.close()


def office(source, target, source_ext, target_ext, executable):
    out = target.parent / "office-output"
    out.mkdir()
    profile = target.parent / "office-profile"
    profile.mkdir()
    # Fresh profile disables macros and external link updates before opening input.
    (profile / "user").mkdir()
    (profile / "user/registrymodifications.xcu").write_text(
        """<?xml version="1.0"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>""",
        encoding="utf-8",
    )
    spec = target_ext + ":" + OFFICE_FILTERS[group(source_ext)][target_ext]
    input_filter = {
        "txt": "Text (encoded):UTF8",
        "csv": "Text - txt - csv (StarCalc):44,34,76",
        "html": "HTML (StarWriter)",
    }.get(source_ext)
    subprocess.run(
        [
            executable,
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            *(["--infilter=" + input_filter] if input_filter else []),
            "--convert-to",
            spec,
            "--outdir",
            str(out),
            str(source),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    result = out / (source.stem + "." + target_ext)
    if not result.is_file() or result.stat().st_size == 0:
        raise ValueError("LibreOffice не смог открыть или преобразовать документ.")
    if target_ext == "html":
        # Preserve exported images and other sidecars as a complete HTML bundle.
        with zipfile.ZipFile(
            target.with_suffix(".zip"), "w", zipfile.ZIP_DEFLATED
        ) as bundle:
            for item in out.rglob("*"):
                if item.is_file():
                    bundle.write(item, item.relative_to(out))
    else:
        result.replace(target)


def media(source, target, source_ext, ext, settings, executable, max_output):
    args = [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
    ]
    if ext in GROUPS["Аудио"]:
        codecs = {
            "mp3": "libmp3lame",
            "wav": "pcm_s16le",
            "ogg": "libvorbis",
            "flac": "flac",
            "m4a": "aac",
        }
        args += ["-map", "0:a:0", "-vn", "-c:a", codecs[ext]]
        if ext not in ("wav", "flac"):
            args += ["-b:a", settings["audio"] + "k"]
    else:
        args += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-sn",
            "-dn",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
        ]
        if ext == "webm":
            args += [
                "-c:v",
                "libvpx-vp9",
                "-crf",
                settings["video"],
                "-b:v",
                "0",
                "-deadline",
                "good",
                "-cpu-used",
                "4",
                "-c:a",
                "libopus",
            ]
        elif ext == "avi":
            q = {"18": "2", "23": "5", "28": "8"}[settings["video"]]
            args += ["-c:v", "mpeg4", "-q:v", q, "-c:a", "libmp3lame"]
        else:
            args += [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                settings["video"],
                "-c:a",
                "aac",
            ]
        args += ["-b:a", settings["audio"] + "k"]
        if ext in ("mp4", "mov"):
            args += ["-movflags", "+faststart"]
    # Monitor in the parent rejects oversized results; do not produce truncated successful output.
    args += ["-map_metadata", "-1", str(target)]
    result = subprocess.run(
        args,
        capture_output=True,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode:
        raise ValueError(
            "Не удалось прочитать медиафайл или отсутствует нужная дорожка/кодек."
        )


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    source = Path(job["source"])
    try:
        if job.get("action") == "inspect":
            info = inspect_file(source, job["source_ext"], job["ffmpeg"])
            Path(job["result"]).write_text(
                json.dumps({"inspection": info}), encoding="utf-8"
            )
            return
        target = Path(job["target"])
        ext, target_ext = job["source_ext"], job["target_ext"]
        validate(source, ext)
        if group(ext) == "Изображения":
            image(source, target, target_ext, job["settings"]["image"])
        elif ext == "pdf":
            pdf(source, target, target_ext)
        elif group(ext) in ("Аудио", "Видео"):
            media(
                source,
                target,
                ext,
                target_ext,
                job["settings"],
                job["ffmpeg"],
                job["max_output"],
            )
        else:
            office(source, target, ext, target_ext, job["office"])
        result = target.with_suffix(".zip") if target_ext == "html" else target
        if not result.is_file() or not result.stat().st_size:
            raise ValueError("Преобразование создало пустой файл.")
        Path(job["result"]).write_text(
            json.dumps({"path": str(result)}), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - isolate failures from third-party decoders
        # Keep OS paths, converter diagnostics and document contents out of Telegram.
        if isinstance(exc, (ValueError, InvalidFile)):
            message = str(exc)[:500]
        elif isinstance(exc, ImportError):
            message = "Не установлен Python-модуль конвертера. Сообщите владельцу бота."
        elif isinstance(exc, FileNotFoundError):
            message = "Программа конвертации недоступна. Сообщите владельцу бота."
        else:
            message = "Не удалось прочитать файл. Он повреждён, защищён или содержит неподдерживаемые данные."
        Path(job["result"]).write_text(json.dumps({"error": message}), encoding="utf-8")
        sys.exit(1)


if __name__ == "__main__":
    main()
