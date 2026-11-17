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
        allowed = (
            "instagram.com",
            "youtube.com",
            "youtu.be",
        )
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
            stderr.decode(errors="replace")[-1500:]
        )

    return stdout


async def start(update, context):
    await update.message.reply_text(
        "Salom! 👋\n\n"
        "Menga Instagram Reels yoki YouTube Shorts "
        "havolasini yubor.\n\n"
        "Yoki video fayl yubor.\n"
        "Men uni MP3 audio qilib beraman. 🎵"
    )


async def make_mp3(update, context, url=None):
    message = update.message

    status = await message.reply_text(
        "⏳ Video qabul qilindi. MP3 tayyorlayapman..."
    )

    try:
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            output = folder / "audio.mp3"

            if url:
                await run_command(
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "--extract-audio",
                    "--audio-format", "mp3",
                    "--audio-quality", "4",
                    "--max-filesize",
                    f"{MAX_UPLOAD_MB}M",
                    "-o", str(folder / "audio.%(ext)s"),
                    url,
                )

            else:
                media = message.video or message.document

                if not media:
                    await status.edit_text(
                        "Iltimos, video yubor."
                    )
                    return

                if message.document and not (
                    message.document.mime_type or ""
                ).startswith("video/"):
                    await status.edit_text(
                        "Iltimos, video fayl yubor."
                    )
                    return

                if media.file_size and media.file_size > 20 * 1024 * 1024:
                    await status.edit_text(
                        "Video 20 MB dan kichik bo‘lishi kerak."
                    )
                    return

                source = folder / "video.mp4"
                telegram_file = await context.bot.get_file(
                    media.file_id
                )
                await telegram_file.download_to_drive(
                    custom_path=str(source)
                )

                await run_command(
                    "ffmpeg",
                    "-y",
                    "-i", str(source),
                    "-vn",
                    "-codec:a", "libmp3lame",
                    "-q:a", "4",
                    str(output),
                )

            if not output.exists():
                raise RuntimeError("MP3 fayl yaratilmadi.")

            size = output.stat().st_size

            if size > MAX_UPLOAD_MB * 1024 * 1024:
                await status.edit_text(
                    "MP3 fayl juda katta. Kichikroq video yubor."
                )
                return

            with open(output, "rb") as audio:
                await message.reply_audio(
                    audio=audio,
                    filename="audio.mp3",
                    title="MP3 Audio",
                )

            await status.delete()

    except Exception as error:
        print("PROCESSING ERROR:", str(error))
        await status.edit_text(
            "❌ Videoni qayta ishlashda xatolik yuz berdi.\n"
            "Boshqa video yoki havola yuborib ko‘r."
        )


async def handle_video(update, context):
    await make_mp3(update, context)


async def handle_link(update, context):
    url = update.message.text.strip()

    if not valid_url(url):
        await update.message.reply_text(
            "Instagram yoki YouTube havolasini yubor."
        )
        return

    await make_mp3(update, context, url=url)


bot_app.add_handler(CommandHandler("start", start))

bot_app.add_handler(
    MessageHandler(
        filters.VIDEO | filters.Document.ALL,
        handle_video,
    )
)

bot_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
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
