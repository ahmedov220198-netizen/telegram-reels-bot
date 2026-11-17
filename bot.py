import os
import re
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH = "/telegram"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))

bot_app = Application.builder().token(TOKEN).build()


def valid_url(url):
    try:
        host = urlparse(url).hostname or ""
        allowed = ("instagram.com", "youtube.com", "youtu.be")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in allowed
        )
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
        raise RuntimeError(
            stderr.decode(errors="replace")[-2000:]
        )

    return stdout


async def start(update, context):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Отправь мне ссылку на Instagram Reels "
        "или YouTube Shorts.\n\n"
        "Я скачаю видео и отправлю тебе:\n"
        "🎬 Видео MP4\n"
        "🎵 Аудио MP3\n\n"
        "Также я могу автоматически обрабатывать "
        "ссылки в группе."
    )


async def process_media(update, context, url=None):
    message = update.effective_message

    status = await message.reply_text(
        "⏳ Скачиваю видео и готовлю MP3..."
    )

    try:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source.mp4"
            video = folder / "video.mp4"
            audio = folder / "audio.mp3"

            if url:
                await run_command(
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "-f", "best[ext=mp4]/best",
                    "--merge-output-format", "mp4",
                    "--max-filesize", f"{MAX_UPLOAD_MB}M",
                    "-o", str(folder / "download.%(ext)s"),
                    url,
                )

                downloaded = list(folder.glob("download.*"))

                if not downloaded:
                    raise RuntimeError("Видео не скачано")

                source = downloaded[0]

            else:
                media = message.video or message.document

                if not media:
                    await status.edit_text(
                        "❌ Отправь видеофайл."
                    )
                    return

                if message.document and not (
                    message.document.mime_type or ""
                ).startswith("video/"):
                    await status.edit_text(
                        "❌ Это не видеофайл."
                    )
                    return

                if (
                    media.file_size
                    and media.file_size > MAX_UPLOAD_MB * 1024 * 1024
                ):
                    await status.edit_text(
                        "❌ Видео слишком большое. Максимум 49 МБ."
                    )
                    return

                telegram_file = await context.bot.get_file(
                    media.file_id
                )

                await telegram_file.download_to_drive(
                    custom_path=str(source)
                )

            if source.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError("Видео превышает лимит размера")

            # Prepare MP4 video
            await run_command(
                "ffmpeg",
                "-y",
                "-i", str(source),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(video),
            )

            # Extract MP3 audio
            await run_command(
                "ffmpeg",
                "-y",
                "-i", str(video),
                "-vn",
                "-codec:a", "libmp3lame",
                "-q:a", "4",
                str(audio),
            )

            limit = MAX_UPLOAD_MB * 1024 * 1024

            if video.stat().st_size > limit:
                await status.edit_text(
                    "❌ Видео слишком большое для отправки в Telegram."
                )
                return

            if audio.stat().st_size > limit:
                await status.edit_text(
                    "❌ MP3 слишком большой для отправки."
                )
                return

            # Send video
            with open(video, "rb") as video_file:
                await message.reply_video(
                    video=video_file,
                    supports_streaming=True,
                    caption="🎬 Готово! Видео MP4"
                )

            # Send audio
            with open(audio, "rb") as audio_file:
                await message.reply_audio(
                    audio=audio_file,
                    filename="audio.mp3",
                    title="MP3 Audio"
                )

            await status.delete()

    except Exception as error:
        print("PROCESSING ERROR:", str(error))
        await status.edit_text(
            "❌ Не удалось обработать видео.\n"
            "Попробуй другую ссылку или видеофайл."
        )


async def handle_video(update, context):
    await process_media(update, context)


async def handle_link(update, context):
    message = update.effective_message
    text = message.text or message.caption or ""

    urls = re.findall(r'https?://[^\s<>]+', text)

    for url in urls:
        url = url.rstrip(".,!?;:)]}")

        if valid_url(url):
            await process_media(update, context, url=url)
            return


bot_app.add_handler(CommandHandler("start", start))

bot_app.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.ALL,
        handle_video,
    )
)

bot_app.add_handler(
    MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        handle_link,
    )
)


@asynccontextmanager
async def lifespan(app):
    await bot_app.initialize()
    await bot_app.start()

    await bot_app.bot.set_webhook(
        BASE_URL + WEBHOOK_PATH
    )

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
