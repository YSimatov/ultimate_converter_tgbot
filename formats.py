GROUPS = {
    "Документы": ("pdf", "docx", "doc", "txt", "rtf", "odt", "html"),
    "Таблицы": ("xlsx", "xls", "csv", "ods"),
    "Презентации": ("pptx", "ppt", "odp"),
    "Изображения": ("jpg", "png", "webp", "bmp", "tiff"),
    "Аудио": ("mp3", "wav", "ogg", "flac", "m4a"),
    "Видео": ("mp4", "mov", "mkv", "webm", "avi"),
}
ALIASES = {"jpeg": "jpg", "tif": "tiff", "htm": "html"}
ALL = {x for values in GROUPS.values() for x in values}


def normalize(ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return ALIASES.get(ext, ext)


def group(ext: str) -> str:
    return next((name for name, values in GROUPS.items() if ext in values), "")


def targets(
    ext: str,
    office: bool,
    ffmpeg: bool,
    *,
    has_audio: bool = True,
    has_video: bool = True,
    pdf_text: bool = True,
) -> list[str]:
    category = group(ext)
    if ext == "pdf":
        return ["docx", "txt"] if pdf_text else []
    if category == "Изображения":
        return [x for x in GROUPS[category] if x != ext]
    if category in ("Аудио", "Видео"):
        if not ffmpeg:
            return []
        values = (() if not has_video else GROUPS["Видео"]) + (
            GROUPS["Аудио"] if has_audio else ()
        )
        if category == "Аудио":
            values = GROUPS["Аудио"] if has_audio else ()
        return [x for x in values if x != ext]
    if category and office:
        values = GROUPS[category] + (("pdf",) if category != "Документы" else ())
        return [x for x in values if x != ext]
    return []
