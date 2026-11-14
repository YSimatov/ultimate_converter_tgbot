import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from config import ROOT
from formats import targets


class ConversionError(Exception):
    pass


async def kill_tree(process):
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


async def run_worker(source: Path, payload: dict, config) -> dict:
    """Bound both conversion and inspection by the same process/timeout limits."""
    result = source.parent / "result.json"
    job = source.parent / "job.json"
    result.unlink(missing_ok=True)
    job.write_text(
        json.dumps(
            {
                "source": str(source.resolve()),
                **payload,
                "office": config.office,
                "ffmpeg": config.ffmpeg,
                "max_output": config.max_output,
                "result": str(result.resolve()),
            }
        ),
        encoding="utf-8",
    )
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with (source.parent / "worker.log").open("wb") as log:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "worker.py"),
                str(job.resolve()),
                stdout=log,
                stderr=log,
                **options,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Cancellation can arrive while Windows is still creating the process.
            # Reap it before allowing the caller to remove the input directory.
            try:
                process = await spawn
            except OSError:
                pass
            else:
                await kill_tree(process)
            raise
        try:
            async with asyncio.timeout(config.timeout):
                while process.returncode is None:
                    await asyncio.sleep(0.25)

                    def oversized():
                        try:
                            return any(
                                p.is_file() and p.stat().st_size > config.max_output
                                for p in source.parent.rglob("*")
                                if p != source
                            )
                        except FileNotFoundError:
                            return (
                                False  # A converter may rename its own temporary files.
                            )

                    if await asyncio.to_thread(oversized):
                        raise ConversionError(
                            "Результат превышает лимит отправки. Уменьшите качество."
                        )
                await process.wait()
        except TimeoutError:
            await kill_tree(process)
            raise ConversionError(
                "Время конвертации истекло. Попробуйте меньший файл."
            ) from None
        except BaseException:
            await kill_tree(process)
            raise
    if not result.exists():
        raise ConversionError(
            "Конвертер завершился с ошибкой. Возможно, файл повреждён."
        )
    response = json.loads(result.read_text(encoding="utf-8"))
    if "error" in response:
        raise ConversionError(response["error"])
    return response


async def inspect_file(source: Path, hint: str, config) -> dict:
    response = await run_worker(
        source, {"action": "inspect", "source_ext": hint}, config
    )
    return response["inspection"]


async def convert(
    source: Path, source_ext: str, target_ext: str, settings: dict, config
) -> Path:
    if target_ext not in targets(source_ext, bool(config.office), bool(config.ffmpeg)):
        raise ConversionError("Этот формат недоступен. Загрузите файл заново.")
    target = source.parent / ("converted." + target_ext)
    response = await run_worker(
        source,
        {
            "action": "convert",
            "target": str(target.resolve()),
            "source_ext": source_ext,
            "target_ext": target_ext,
            "settings": settings,
        },
        config,
    )
    output = Path(response["path"])
    if not output.resolve().is_relative_to(source.parent.resolve()):
        raise ConversionError("Конвертер вернул некорректный путь результата.")
    if output.stat().st_size > config.max_output:
        raise ConversionError("Результат превышает лимит отправки. Уменьшите качество.")
    return output
