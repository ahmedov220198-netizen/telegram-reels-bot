import os
import re
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager

import yt_dlp
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH = "/telegram"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_BYTES = MAX_MB * 1024 * 1024

bot = Application.builder().token(TOKEN).build()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Salom! 👋\n\n"
        "Menga YouTube Shorts yoki Instagram Reels havolasini yubor.\n"
        "Men videoni MP4 va audiosini MP3 qilib yuboraman."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


def download_video(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_BYTES,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))

    if not path.exists():
        files = list(Path(folder).glob("video.*"))
        if not files:
            raise RuntimeError("Video file not found")
        path = files[0]

    if path.stat().st_size > MAX_BYTES:
        raise RuntimeError("Video exceeds Telegram upload limit")

    audio = Path(folder) / "audio.mp3"

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(path),
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "4",
            str(audio)
        ],
        check=True,
        capture_output=True,
        timeout=180
    )

    return path, audio


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    message = update.effective_message

    if not message or not message.text:
        return

    match = re.search(r"https?://\S+", message.text)

    if not match:
        await message.reply_text(
            "Menga YouTube yoki Instagram havolasini yubor."
        )
        return

    url = match.group(0).rstrip(".,!?)]}")
    host = (urlparse(url).hostname or "").lower()

    if not (
        host == "youtu.be"
        or host.endswith("youtube.com")
        or host.endswith("instagram.com")
    ):
        await message.reply_text("Faqat YouTube va Instagram havolalari.")
        return

    status = await message.reply_text("⏳ Yuklanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            video, audio = await asyncio.to_thread(
                download_video, url, folder
            )

            with open(video, "rb") as file:
                await message.reply_video(
                    video=file,
                    caption="🎬 Video",
                    read_timeout=180,
                    write_timeout=180
                )

            if audio.exists() and audio.stat().st_size <= MAX_BYTES:
                with open(audio, "rb") as file:
                    await message.reply_audio(
                        audio=file,
                        filename="audio.mp3",
                        title="MP3",
                        read_timeout=180,
                        write_timeout=180
                    )

        await status.delete()

    except Exception as error:
        log.exception("Download error")
        await status.edit_text(
            "❌ Yuklab olishda xatolik.\n\n"
            + str(error)[:800]
        )


bot.add_handler(CommandHandler("start", start))
bot.add_handler(CommandHandler("help", help_command))
bot.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.initialize()
    await bot.start()

    await bot.bot.set_webhook(
        url=BASE_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True
    )

    log.info("Telegram webhook is active")

    yield

    await bot.bot.delete_webhook()
    await bot.stop()
    await bot.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return {"status": "live", "bot": "running"}


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):

    if WEBHOOK_SECRET:
        secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403)

    try:
        data = await request.json()
        update = Update.de_json(data, bot.bot)
        await bot.process_update(update)
        return {"ok": True}

    except Exception:
        log.exception("Webhook error")
        raise HTTPException(status_code=500)
