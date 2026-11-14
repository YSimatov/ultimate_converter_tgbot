import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def program(env: str, names: tuple[str, ...], candidates=()) -> str | None:
    explicit = os.getenv(env, "").strip().strip('"')
    if explicit:
        return explicit if Path(explicit).is_file() else shutil.which(explicit)
    for name in names:
        if found := shutil.which(name):
            return found
    return next((str(p) for p in candidates if p.is_file()), None)


def find_ffmpeg() -> str | None:
    found = program(
        "FFMPEG_PATH",
        ("ffmpeg",),
        (Path(os.getenv("ProgramFiles", "C:/Program Files")) / "ShareX/ffmpeg.exe",),
    )
    if found or os.getenv("FFMPEG_PATH"):
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


@dataclass
class Config:
    token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())
    data: Path = field(default_factory=lambda: ROOT / os.getenv("DATA_DIR", "data"))
    max_file: int = field(
        default_factory=lambda: min(int(os.getenv("MAX_FILE_MB", "20")), 20) * 1024**2
    )
    max_output: int = field(
        default_factory=lambda: min(int(os.getenv("MAX_OUTPUT_MB", "49")), 49) * 1024**2
    )
    timeout: int = field(
        default_factory=lambda: int(os.getenv("CONVERSION_TIMEOUT", "180"))
    )
    concurrent: int = field(
        default_factory=lambda: int(os.getenv("MAX_CONCURRENT", "2"))
    )
    pending: int = field(default_factory=lambda: int(os.getenv("MAX_PENDING", "8")))
    ttl: int = field(default_factory=lambda: int(os.getenv("FILE_TTL_SECONDS", "900")))
    office: str | None = field(
        default_factory=lambda: program(
            "LIBREOFFICE_PATH",
            ("soffice.com", "soffice"),
            (
                Path(os.getenv("ProgramFiles", "C:/Program Files"))
                / "LibreOffice/program/soffice.com",
                Path(os.getenv("ProgramFiles", "C:/Program Files"))
                / "LibreOffice/program/soffice.exe",
            ),
        )
    )
    ffmpeg: str | None = field(default_factory=find_ffmpeg)

    def __post_init__(self):
        if (
            min(
                self.max_file,
                self.max_output,
                self.timeout,
                self.concurrent,
                self.pending,
                self.ttl,
            )
            <= 0
        ):
            raise ValueError("Лимиты в .env должны быть положительными.")
