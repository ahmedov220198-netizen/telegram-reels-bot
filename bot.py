import os
import re
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH = "/telegram"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

bot_app = Application.builder().token(TOKEN).build()


def valid_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        allowed = ("instagram.com", "youtube.com", "youtu.be")
        return any(host == domain or host.endswith("." + domain) for domain in allowed)
    except Exception:
        return False


async def run_command(*args):
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace")[-2500:])
    return stdout


async def start(update, context):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Привет! 👋\n\n"
            "Отправь ссылку на Instagram Reels или YouTube Shorts.\n"
            "Я постараюсь скачать видео и отправить:\n"
            "🎬 MP4 — видео\n"
            "🎵 MP3 — аудио\n\n"
            "В группе просто отправь ссылку в сообщении."
        )


async def process_media(update, context, url=None):
    message = update.effective_message
    if not message:
        return

    status = await message.reply_text("⏳ Загружаю видео и готовлю MP3...")

    try:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            video = folder / "video.mp4"
            audio = folder / "audio.mp3"

            if url:
                # yt-dlp downloads the best available source; ffmpeg below
                # converts it to a Telegram-friendly MP4.
                await run_command(
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "--format", "bv*+ba/b",
                    "--merge-output-format", "mp4",
                    "--max-filesize", f"{MAX_UPLOAD_MB}M",
                    "--output", str(folder / "raw.%(ext)s"),
                    url,
                )

                candidates = [
                    p for p in folder.glob("raw.*")
                    if p.is_file() and not p.name.endswith((".part", ".ytdl"))
                ]
                if not candidates:
                    raise RuntimeError("Не удалось скачать исходное видео.")
                source = candidates[0]

            else:
                media = message.video or message.document
                if not media or (
                    message.document
                    and not (message.document.mime_type or "").startswith("video/")
                ):
                    await status.edit_text("❌ Отправь видеофайл или ссылку.")
                    return

                if media.file_size and media.file_size > MAX_UPLOAD_BYTES:
                    await status.edit_text(
                        f"❌ Видео больше {MAX_UPLOAD_MB} МБ. Отправь файл поменьше."
                    )
                    return

                telegram_file = await context.bot.get_file(media.file_id)
                await telegram_file.download_to_drive(custom_path=str(source))

            if source.stat().st_size > MAX_UPLOAD_BYTES:
                raise RuntimeError(f"Исходное видео больше {MAX_UPLOAD_MB} МБ.")

            # Re-encode to MP4 for compatibility.
            await run_command(
                "ffmpeg", "-y",
                "-i", str(source),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "25",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(video),
            )

            await run_command(
                "ffmpeg", "-y",
                "-i", str(video),
                "-vn",
                "-codec:a", "libmp3lame",
                "-q:a", "4",
                str(audio),
            )

            for path, label in ((video, "Видео"), (audio, "MP3")):
                if not path.exists():
                    raise RuntimeError(f"{label}: файл не создан.")
                if path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise RuntimeError(f"{label}: файл превышает лимит {MAX_UPLOAD_MB} МБ.")

            with open(video, "rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    supports_streaming=True,
                    caption="🎬 Готово! Видео MP4"
                )

            with open(audio, "rb") as audio_file:
                await message.reply_audio(
                    audio=audio_file,
                    filename="audio.mp3",
                    title="MP3 Audio"
                )

            await status.delete()

    except Exception as error:
        error_text = str(error)
        print("PROCESSING ERROR:", error_text)
        if "Sign in to confirm" in error_text or "not a bot" in error_text.lower():
            reply = (
                "❌ YouTube временно блокирует загрузку с сервера Render.\n"
                "Для этого ролика требуется подтверждение YouTube. "
                "Без cookies или другого разрешённого источника бот не сможет "
                "гарантированно скачать его."
            )
        else:
            reply = (
                "❌ Не удалось обработать видео.\n"
                "Попробуй другую ссылку или видеофайл."
            )
        await status.edit_text(reply)


async def handle_video(update, context):
    await process_media(update, context)


async def handle_link(update, context):
    message = update.effective_message
    if not message:
        return

    text = message.text or message.caption or ""
    urls = re.findall(r"https?://[^\s<>]+", text)

    for url in urls:
        url = url.rstrip(".,!?;:)]}")
        if valid_url(url):
            await process_media(update, context, url=url)
            return


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(
    MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video)
)
bot_app.add_handler(
    MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_link)
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(BASE_URL + WEBHOOK_PATH)
    print("Telegram bot started successfully!")
    yield
    await bot_app.bot.delete_webhook()
    await bot_app.stop()
    await bot_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return {"status": "Bot is running"}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok": True}
