import asyncio
import html
import io
import logging
import re
import secrets
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.token import TokenValidationError

from config import Config
from converter import ConversionError, convert, inspect_file
from database import Database
from formats import GROUPS, normalize, targets

log = logging.getLogger(__name__)


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


HOME = keyboard(
    [
        [("✨ Конвертировать", "upload")],
        [("⚙️ Настройки", "settings"), ("🧩 Форматы", "formats")],
        [("🕘 История", "history"), ("💬 Помощь", "help")],
    ]
)
BACK = keyboard([[("‹ Назад", "home"), ("⌂ Главное меню", "home")]])
CANCEL = keyboard([[("✕ Отмена", "cancel"), ("⌂ Главное меню", "home")]])
WELCOME = (
    "<b>CONVERT · файлы в новом формате</b>\n\n"
    "Документы, таблицы, презентации, изображения, аудио и видео.\n"
    "Отправьте файл — я предложу доступные варианты.\n\n"
    "<i>Один файл. Нужный формат. Готово.</i>"
)


@contextmanager
def instance_lock(path: Path):
    """Prevent a second process from cleaning a running bot's job directories."""
    with path.open("a+b") as handle:
        handle.write(b"\0")
        handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(
                "Бот уже запущен с этой папкой DATA_DIR. Остановите второй экземпляр."
            ) from None
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class Upload:
    path: Path
    name: str
    ext: str
    nonce: str
    created: float
    choices: tuple[str, ...] = ()


class LimitedWriter(io.BufferedWriter):
    """Stop downloads at the byte limit, even if metadata is inaccurate."""

    def __init__(self, path: Path, limit: int):
        super().__init__(path.open("wb", buffering=0))
        self.limit = limit
        self.received = 0

    def write(self, data):
        if self.received + len(data) > self.limit:
            raise ConversionError("Файл превышает допустимый размер.")
        size = super().write(data)
        self.received += size
        return size


class Application:
    def __init__(self, config):
        self.config = config
        self.db = Database(config.data / "bot.sqlite3")
        self.uploads: dict[int, Upload] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.gate = asyncio.Semaphore(config.concurrent)
        self.dp = Dispatcher()
        self.dp.message.register(self.start, CommandStart())
        self.dp.message.register(self.cancel_command, Command("cancel"))
        self.dp.message.register(self.start, Command("menu"))
        self.dp.callback_query.register(self.callback)
        self.dp.message.register(
            self.receive,
            F.document | F.photo | F.audio | F.video | F.voice | F.animation,
        )
        self.dp.message.register(self.fallback)
        self.dp.errors.register(self.on_error)

    async def on_error(self, event):
        # Do not log Telegram exceptions verbatim: request URLs can contain tokens.
        log.error("Unhandled update error: %s", type(event.exception).__name__)
        message = event.update.message
        if not message and event.update.callback_query:
            message = event.update.callback_query.message
        if isinstance(message, Message):
            try:
                await message.answer(
                    "⚠️ Не удалось выполнить действие. Попробуйте ещё раз.",
                    reply_markup=HOME,
                )
            except TelegramAPIError:
                pass
        return True

    async def discard(self, uid):
        upload = self.uploads.pop(uid, None)
        if upload:
            await self.remove_job(upload.path.parent)

    async def remove_job(self, folder: Path):
        temp = (self.config.data / "temp").resolve()
        resolved = folder.resolve()
        if (
            resolved.parent != temp
            or not folder.name.startswith("job-")
            or folder.is_symlink()
            or folder.is_junction()
        ):
            log.error("Refused cleanup outside application job directory")
            return
        try:
            await asyncio.to_thread(shutil.rmtree, folder)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Temporary directory cleanup deferred until restart")

    async def start(self, message: Message):
        if message.chat.type != "private":
            await message.answer("Откройте личный чат с ботом для конвертации файлов.")
            return
        await message.answer(WELCOME, reply_markup=HOME)

    async def cancel(self, uid):
        task = self.tasks.get(uid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.tasks.get(uid) is task:
                self.tasks.pop(uid, None)
        await self.discard(uid)

    async def cancel_command(self, message: Message):
        await self.cancel(message.from_user.id)
        await message.answer(
            "✕ Операция отменена. Можно начать заново.", reply_markup=HOME
        )

    async def fallback(self, message: Message):
        await message.answer(
            "📎 Отправьте файл как документ или выберите раздел меню.",
            reply_markup=HOME,
        )

    async def show(self, message, text, markup=BACK):
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                await message.answer(text, reply_markup=markup)

    async def callback(self, call: CallbackQuery):
        if not isinstance(call.message, Message) or call.message.chat.type != "private":
            await call.answer("Откройте личный чат с ботом.", show_alert=True)
            return
        uid, data, message = call.from_user.id, call.data or "", call.message
        await call.answer()
        if data == "cancel":
            await self.cancel(uid)
            await self.show(message, "✕ Операция отменена.", HOME)
        elif data == "home":
            await self.show(message, WELCOME, HOME)
        elif data == "upload":
            await self.show(
                message,
                "<b>✨ Новая конвертация</b>\n\nОтправьте файл в этот чат.\n"
                f"Лимит загрузки: {self.config.max_file // 1024**2} МБ.\n"
                "Для фото без сжатия выберите «Отправить как файл».",
                CANCEL,
            )
        elif data == "settings" or data.startswith("set:"):
            if data.startswith("set:"):
                parts = data.split(":")
                if len(parts) != 3:
                    return
                try:
                    await self.db.set(uid, parts[1], parts[2])
                except ValueError:
                    return
            settings = await self.db.settings(uid)
            rows = []
            for key, values, labels in (
                ("image", ("70", "90", "100"), ("70%", "90%", "100%")),
                ("audio", ("128", "192", "320"), ("128k", "192k", "320k")),
                ("video", ("28", "23", "18"), ("Компактно", "Баланс", "Лучшее")),
            ):
                rows.append(
                    [
                        (
                            ("✓ " if settings[key] == val else "") + label,
                            f"set:{key}:{val}",
                        )
                        for val, label in zip(values, labels, strict=True)
                    ]
                )
            rows += [[("‹ Назад", "home"), ("⌂ Главное меню", "home")]]
            await self.show(
                message,
                "<b>⚙️ Качество результата</b>\n\n"
                "🖼 Изображения · JPG / WEBP\n🎧 Аудио · битрейт сжатых форматов\n"
                "🎬 Видео · размер и детализация\n\n"
                "<i>PNG, BMP, TIFF, WAV и FLAC сохраняются без потерь кодирования. "
                "Качество исходника улучшить невозможно.</i>",
                keyboard(rows),
            )
        elif data == "formats":
            text = "<b>🧩 Форматы и доступность</b>\n\n"
            for label, extensions in GROUPS.items():
                text += (
                    f"<b>{label}</b>\n"
                    + " · ".join(x.upper() for x in extensions)
                    + "\n\n"
                )
            text += (
                "PDF → DOCX / TXT (текстовый слой).\nТаблицы и презентации → PDF.\n"
                "Видео → видео / аудио. Остальные — внутри своей группы.\n\n"
            )
            text += "LibreOffice: " + (
                "✅" if self.config.office else "❌ не установлен"
            )
            text += "\nFFmpeg: " + ("✅" if self.config.ffmpeg else "❌ не установлен")
            text += "\n\nКнопки показывают только доступные на этом компьютере преобразования."
            await self.show(message, text)
        elif data == "help":
            await self.show(
                message,
                "<b>💬 Как это работает</b>\n\n"
                "1. Отправьте файл.\n2. Выберите формат.\n3. Скачайте результат.\n\n"
                "Настройки применяются при начале задания. Отмена останавливает обработку. "
                "Главное меню позволяет просматривать разделы во время конвертации.\n\n"
                "Сканы PDF требуют OCR (не поддерживается). Вёрстка документов может измениться. "
                "CSV хранит один лист без оформления. HTML возвращается ZIP-архивом с ресурсами. "
                "Анимации и многостраничные TIFF конвертируются по первому кадру.\n\n"
                "Файлы удаляются после задания; неиспользованные — через 15 минут по умолчанию. "
                "История хранит только имена, форматы, время и статус (до 100 записей). "
                "Telegram хранит сообщения по своим правилам.\n\n"
                "/start · /menu · /cancel",
            )
        elif data in ("history", "clear_history"):
            if data == "clear_history":
                await self.db.clear(uid)
            rows = await self.db.history(uid)
            text = "<b>🕘 Последние конвертации</b>\n<i>Время UTC</i>\n\n"
            icons = {"ok": "✅", "error": "⚠️", "cancelled": "✕"}
            for row in rows:
                text += (
                    f"{icons.get(row['status'], '•')} {html.escape(row['name'][:60])}\n"
                )
                text += f"{row['source'].upper()} → {row['target'].upper()} · {row['created']}\n\n"
            if not rows:
                text += "Пока пусто. Отправьте первый файл ✨"
            await self.show(
                message,
                text,
                keyboard(
                    [
                        [("🗑 Очистить историю", "clear_history")],
                        [("‹ Назад", "home"), ("⌂ Главное меню", "home")],
                    ]
                ),
            )
        elif data.startswith("convert:"):
            parts = data.split(":")
            if len(parts) != 3:
                return
            upload = self.uploads.get(uid)
            if (
                not upload
                or upload.nonce != parts[1]
                or time.monotonic() - upload.created > self.config.ttl
            ):
                await message.answer(
                    "⌛ Файл или кнопка устарели. Отправьте файл заново.",
                    reply_markup=HOME,
                )
                return
            if uid in self.tasks:
                await message.answer("⏳ Задание уже выполняется.", reply_markup=CANCEL)
                return
            if len(self.tasks) >= self.config.pending:
                await message.answer(
                    "⏳ Очередь заполнена. Попробуйте немного позже.", reply_markup=HOME
                )
                return
            if parts[2] not in upload.choices:
                return
            self.tasks[uid] = asyncio.create_task(
                self.run_job(message, uid, upload, parts[2])
            )

    async def receive(self, message: Message):
        if message.chat.type != "private":
            return
        uid = message.from_user.id
        if uid in self.tasks:
            await message.answer(
                "⏳ Сначала дождитесь результата или отмените задание.",
                reply_markup=CANCEL,
            )
            return
        if len(self.tasks) >= self.config.pending:
            await message.answer(
                "⏳ Очередь заполнена. Попробуйте немного позже.", reply_markup=HOME
            )
            return
        if len(self.uploads) >= self.config.pending * 4 and uid not in self.uploads:
            await message.answer(
                "⏳ Сейчас много файлов. Попробуйте позже.", reply_markup=HOME
            )
            return
        # Reserve the user before any await to prevent concurrent uploads/conversions.
        self.tasks[uid] = asyncio.current_task()
        folder = None
        try:
            media = (
                message.document
                or message.audio
                or message.video
                or message.voice
                or message.animation
            )
            if message.photo:
                media, name = message.photo[-1], "photo.jpg"
            else:
                name = getattr(media, "file_name", None) or (
                    "voice.ogg"
                    if message.voice
                    else "audio.mp3"
                    if message.audio
                    else "video.mp4"
                )
            ext = normalize(Path(name).suffix)
            if not media.file_size or media.file_size > self.config.max_file:
                await message.answer(
                    f"📦 Файл должен быть не больше {self.config.max_file // 1024**2} МБ.",
                    reply_markup=HOME,
                )
                return
            status = await message.answer("📥 Загружаю файл…", reply_markup=CANCEL)
            folder = Path(
                tempfile.mkdtemp(prefix="job-", dir=self.config.data / "temp")
            )
            path = folder / "upload.bin"
            async with asyncio.timeout(90):
                with LimitedWriter(path, self.config.max_file) as destination:
                    await message.bot.download(
                        media.file_id, destination=destination, timeout=60
                    )
            await self.show(
                status,
                "🔎 <b>Проверяю файл</b>\nОпределяю формат и доступные преобразования…",
                CANCEL,
            )
            async with self.gate:
                info = await inspect_file(path, ext, self.config)
            ext = info.pop("ext")
            choices = targets(
                ext, bool(self.config.office), bool(self.config.ffmpeg), **info
            )
            if not choices:
                if ext == "pdf" and not info["pdf_text"]:
                    raise ConversionError(
                        "В PDF нет текстового слоя. Для скана требуется OCR, он не поддерживается."
                    )
                raise ConversionError(
                    "Для этого файла сейчас нет доступных преобразований. Проверьте раздел «Форматы»."
                )
            path = path.rename(folder / ("input." + ext))
            await self.discard(uid)
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:150]
            upload = Upload(
                path,
                safe_name,
                ext,
                secrets.token_hex(4),
                time.monotonic(),
                tuple(choices),
            )
            self.uploads[uid] = upload
            folder = None
            buttons = [(x.upper(), f"convert:{upload.nonce}:{x}") for x in choices]
            rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
            rows += [
                [("‹ Назад", "upload"), ("✕ Отмена", "cancel")],
                [("⌂ Главное меню", "home")],
            ]
            await self.show(
                status,
                f"<b>📎 {html.escape(safe_name)}</b>\n"
                f"{ext.upper()} · {path.stat().st_size / 1024**2:.2f} МБ\n\n"
                "<b>Во что конвертировать?</b>",
                keyboard(rows),
            )
        except asyncio.CancelledError:
            raise
        except ConversionError as exc:
            await message.answer("⚠️ " + html.escape(str(exc)), reply_markup=HOME)
        except (TimeoutError, TelegramAPIError, OSError):
            await message.answer(
                "⚠️ Не удалось загрузить файл. Проверьте размер и повторите.",
                reply_markup=HOME,
            )
        finally:
            if folder:
                await self.remove_job(folder)
            if self.tasks.get(uid) is asyncio.current_task():
                self.tasks.pop(uid, None)

    async def run_job(self, message, uid, upload, target):
        state = "error"
        try:
            settings = await self.db.settings(uid)
            await self.show(
                message, "⏳ <b>Файл в очереди</b>\nСкоро начну обработку.", CANCEL
            )
            async with self.gate:
                await self.show(
                    message,
                    f"⚡ <b>Конвертирую {upload.ext.upper()} → {target.upper()}</b>\n"
                    "Это может занять некоторое время…",
                    CANCEL,
                )
                output = await convert(
                    upload.path, upload.ext, target, settings, self.config
                )
            await self.show(message, "📤 <b>Готово. Отправляю результат…</b>", CANCEL)
            filename = Path(upload.name).stem + output.suffix
            await message.answer_document(
                FSInputFile(output, filename=filename),
                caption=f"✅ <b>Готово</b> · {upload.ext.upper()} → {target.upper()}",
                reply_markup=HOME,
                request_timeout=120,
            )
            state = "ok"
            await self.show(
                message,
                "✅ <b>Конвертация завершена</b>\nРезультат отправлен ниже.",
                HOME,
            )
        except asyncio.CancelledError:
            state = "cancelled"
            raise
        except ConversionError as exc:
            await self.show(
                message,
                "⚠️ <b>Не удалось конвертировать</b>\n\n" + html.escape(str(exc)),
                HOME,
            )
        except Exception as exc:  # noqa: BLE001 - user task must not crash the dispatcher
            log.error("Job failed: %s", type(exc).__name__)
            try:
                await message.answer(
                    "⚠️ Ошибка обработки или отправки. Попробуйте ещё раз.",
                    reply_markup=HOME,
                )
            except TelegramAPIError:
                pass
        finally:
            try:
                await self.db.record(uid, upload.name, upload.ext, target, state)
            finally:
                await self.discard(uid)
                self.tasks.pop(uid, None)

    async def sweep(self):
        while True:
            await asyncio.sleep(30)
            for uid, upload in list(self.uploads.items()):
                if (
                    uid not in self.tasks
                    and time.monotonic() - upload.created > self.config.ttl
                ):
                    await self.discard(uid)

    async def run(self):
        self.config.data.mkdir(parents=True, exist_ok=True)
        with instance_lock(self.config.data / "bot.lock"):
            await self.serve()

    async def serve(self):
        temp = self.config.data / "temp"
        temp.mkdir(exist_ok=True)
        # Remove only this application's abandoned job directories after a restart.
        for folder in temp.glob("job-*"):
            if folder.is_dir():
                await self.remove_job(folder)
        await self.db.init()
        bot = Bot(
            self.config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        sweep = asyncio.create_task(self.sweep())
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="menu", description="Открыть меню"),
                    BotCommand(command="cancel", description="Отменить задание"),
                ]
            )
            await self.dp.start_polling(
                bot, close_bot_session=False, tasks_concurrency_limit=64
            )
        finally:
            sweep.cancel()
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(sweep, *tasks, return_exceptions=True)
            for uid in list(self.uploads):
                await self.discard(uid)
            await bot.session.close()


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if sys.version_info < (3, 12):
        raise SystemExit("Требуется Python 3.12 или новее.")
    try:
        config = Config()
    except ValueError:
        raise SystemExit(
            "Некорректные лимиты в .env. Проверьте числовые настройки по README."
        ) from None
    if not config.token or config.token == "put_your_bot_token_here":
        raise SystemExit("Заполните BOT_TOKEN в .env (см. .env.example).")
    try:
        asyncio.run(Application(config).run())
    except KeyboardInterrupt:
        pass
    except TelegramAPIError as exc:
        raise SystemExit(
            f"Telegram недоступен или токен неверен ({type(exc).__name__})."
        ) from None
    except TokenValidationError:
        raise SystemExit(
            "Некорректный BOT_TOKEN. Укажите токен, полученный у BotFather."
        ) from None


if __name__ == "__main__":
    main()
