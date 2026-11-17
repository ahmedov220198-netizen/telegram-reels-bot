import os
import tempfile
import subprocess
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH = "/telegram"

bot_app = Application.builder().token(TOKEN).build()


async def start(update, context):
    await update.message.reply_text(
        "Salom! Menga video yubor. Men undan MP3 audio tayyorlayman."
    )


async def convert_video(update, context):
    message = update.message
    media = message.video or message.document

    if not media:
        return

    if media.file_size and media.file_size > 20 * 1024 * 1024:
        await message.reply_text("Video hajmi 20 MB dan kichik bo‘lishi kerak.")
        return

    if message.document and not (
        message.document.mime_type or ""
    ).startswith("video/"):
        await message.reply_text("Iltimos, video fayl yubor.")
        return

    await message.reply_text("Video qabul qilindi. MP3 tayyorlayapman...")

    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "video.mp4")
        output = os.path.join(folder, "audio.mp3")

        file = await context.bot.get_file(media.file_id)
        await file.download_to_drive(source)

        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", source,
                "-vn", "-codec:a", "libmp3lame",
                "-q:a", "4", output
            ],
            capture_output=True
        )

        if result.returncode != 0:
            await message.reply_text("Videoni o‘zgartira olmadim.")
            return

        with open(output, "rb") as audio:
            await message.reply_audio(audio=audio, filename="audio.mp3")


bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(
    MessageHandler(filters.VIDEO | filters.Document.ALL, convert_video)
)


@asynccontextmanager
async def lifespan(app):
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.bot.set_webhook(BASE_URL + WEBHOOK_PATH)
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
