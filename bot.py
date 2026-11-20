import os
import re
import asyncio
import tempfile
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import yt_dlp


# =========================
# AYARLAR
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "")
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))

WEBHOOK_PATH = "/telegram"

telegram_app = Application.builder().token(TOKEN).build()

DOWNLOAD_LOCK = asyncio.Lock()

URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:instagram\.com/(?:reel|reels|p)/[^\s]+|"
    r"youtube\.com/shorts/[^\s]+|youtu\.be/[^\s]+)",
    re.IGNORECASE,
)


# =========================
# MESAJLAR
# =========================

WELCOME = (
    "🇹🇷 Merhaba! Reels & Shorts botuna hoş geldin.\n\n"
    "Botu kullanmak için önce grubumuza katıl:\n"
    "Katıldıktan sonra aşağıdaki kontrol düğmesine bas.\n\n"
    "Videoları indirmek için bağlantıyı doğrudan gruba gönder.\n\n"
    "🇷🇺 Привет! Добро пожаловать в бот Reels & Shorts.\n\n"
    "Сначала вступи в нашу группу.\n"
    "После вступления нажми кнопку проверки.\n\n"
    "Чтобы скачать видео, отправь ссылку прямо в нашу группу."
)

JOIN_TEXT = (
    "🇹🇷 Önce Reels & Shorts grubuna katılmalısın.\n\n"
    "🇷🇺 Сначала вступи в группу Reels & Shorts."
)

SUCCESS_TEXT = (
    "🇹🇷 Üyeliğin doğrulandı! Artık bağlantıları grubumuza gönderebilirsin.\n\n"
    "🇷🇺 Подписка подтверждена! Теперь отправляй ссылки в нашу группу."
)


# =========================
# /START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []

    if GROUP_INVITE_LINK:
        keyboard.append([
            InlineKeyboardButton(
                "🇹🇷 Gruba Katıl / 🇷🇺 Вступить в группу",
                url=GROUP_INVITE_LINK,
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "✅ Katıldım – Kontrol Et / Проверить",
            callback_data="check_membership",
        )
    ])

    await update.effective_message.reply_text(
        WELCOME,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================
# GRUP ÜYELİĞİ
# =========================

async def check_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if not GROUP_CHAT_ID:
        await query.message.reply_text(
            "Grup ayarı henüz tamamlanmadı. / Настройки группы ещё не завершены."
        )
        return

    try:
        member = await context.bot.get_chat_member(
            chat_id=int(GROUP_CHAT_ID),
            user_id=query.from_user.id,
        )

        is_member = (
            member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.OWNER,
            )
            or (
                member.status == ChatMemberStatus.RESTRICTED
                and member.is_member
            )
        )

        await query.message.reply_text(
            SUCCESS_TEXT if is_member else JOIN_TEXT
        )

    except Exception:
        logger.exception("Membership check failed")
        await query.message.reply_text(
            "Üyelik kontrolü yapılamadı. Daha sonra tekrar dene.\n"
            "Проверка не удалась. Попробуй позже."
        )


# =========================
# VİDEO İNDİRME MOTORU
# =========================

def download_video(url: str, folder: str):

    options = {
        "outtmpl": os.path.join(folder, "video.%(ext)s"),

        # Basit MP4 format seçimi
        "format": "best[ext=mp4]/best",

        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "max_filesize": MAX_UPLOAD_MB * 1024 * 1024,

        "socket_timeout": 45,

        # Yeniden deneme
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 5,

        # İstek aralıkları
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "sleep_interval_requests": 2,

        "concurrent_fragment_downloads": 1,

        # User-Agent
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if not name.endswith((".part", ".ytdl"))
    ]

    if not files:
        raise RuntimeError("Video dosyası oluşturulamadı.")

    files.sort(
        key=lambda path: path.lower().endswith(".mp4"),
        reverse=True,
    )

    video_path = files[0]

    if os.path.getsize(video_path) > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError("Video Telegram gönderim sınırını aşıyor.")

    return video_path


# =========================
# GRUP LİNKLERİNİ ALGILA
# =========================

async def handle_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = update.effective_message

    if not message or not message.text:
        return

    match = URL_PATTERN.search(message.text)

    if not match:
        return

    url = match.group(0).rstrip(".,!?)\"]}")

    status_message = await message.reply_text(
        "⏳ Video indiriliyor, lütfen bekle...\n"
        "⏳ Видео загружается, пожалуйста, подожди..."
    )

    try:
        async with DOWNLOAD_LOCK:

            await asyncio.sleep(3)

            with tempfile.TemporaryDirectory() as folder:

                video_path = await asyncio.to_thread(
                    download_video,
                    url,
                    folder,
                )

                with open(video_path, "rb") as video_file:

                    await context.bot.send_video(
                        chat_id=message.chat_id,
                        video=video_file,
                        supports_streaming=True,
                        caption=(
                            "✅ Video hazır!\n"
                            "🇹🇷 İyi seyirler!\n"
                            "🇷🇺 Приятного просмотра!"
                        ),
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )

        await status_message.delete()

    except Exception as error:

        logger.exception("Video download failed: %s", url)

        error_text = str(error).lower()

        if "429" in error_text or "too many requests" in error_text:
            user_message = (
                "⚠️ Instagram şu anda indirme isteklerini sınırlıyor.\n"
                "Bir süre bekleyip tekrar dene.\n\n"
                "⚠️ Instagram временно ограничил загрузки.\n"
                "Попробуй немного позже."
            )

        elif "max_filesize" in error_text or "file size" in error_text:
            user_message = (
                "❌ Video boyutu Telegram sınırını aşıyor.\n"
                "❌ Размер видео превышает лимит Telegram."
            )

        else:
            user_message = (
                "❌ Video indirilemedi. Bağlantıyı kontrol edip tekrar dene.\n\n"
                "❌ Не удалось скачать видео. Проверь ссылку."
            )

        try:
            await status_message.edit_text(user_message)
        except Exception:
            logger.exception("Could not update status message")


# =========================
# TELEGRAM HANDLERS
# =========================

telegram_app.add_handler(
    CommandHandler("start", start)
)

telegram_app.add_handler(
    CallbackQueryHandler(
        check_membership,
        pattern="^check_membership$",
    )
)

telegram_app.add_handler(
    MessageHandler(
        filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
        handle_group_message,
    )
)


# =========================
# WEBHOOK
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):

    await telegram_app.initialize()
    await telegram_app.start()

    await telegram_app.bot.set_webhook(
        url=BASE_URL + WEBHOOK_PATH,
        secret_token=WEBHOOK_SECRET or None,
        allowed_updates=Update.ALL_TYPES,
    )

    logger.info("Telegram bot webhook is active.")

    yield

    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def home():
    return {
        "status": "Reels & Shorts bot is running"
    }


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):

    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if received_secret != WEBHOOK_SECRET:
            raise HTTPException(
                status_code=403,
                detail="Forbidden",
            )

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    await telegram_app.process_update(update)

    return {"ok": True}
