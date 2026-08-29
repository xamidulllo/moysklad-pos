"""Telegram bot — kassaga (Telegram Web App) kiruvchi tugmani ko'rsatadi.

Bot alohida process sifatida emas, FastAPI ilovasi bilan BITTA process ichida,
fon vazifasi (background task) sifatida ishga tushiriladi (main.py'dagi
lifespan orqali) — shunda bepul hosting tarifida (masalan Render) faqat bitta
"Web Service" kifoya, alohida to'lovli "background worker" kerak bo'lmaydi.

Ilova endi shaxsiy MoySklad login/parol so'ramaydi (2026-08-29'dan) — bitta
umumiy MoySklad hisobiga ulangan, faqat kassir ismini so'raydi. Bot faqat
kassani ochish uchun "eshik" vazifasini bajaradi.
"""
import logging
import os
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger("pos.bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Render web-xizmatlar uchun bu avtomatik to'ldiriladi (masalan https://xxx.onrender.com);
# boshqa hostingda PUBLIC_BASE_URL'ni qo'lda belgilash kerak bo'ladi.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

_application: Optional[Application] = None


async def _start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PUBLIC_BASE_URL:
        await update.message.reply_text(
            "Ilova manzili sozlanmagan (PUBLIC_BASE_URL). Administratorga murojaat qiling."
        )
        return
    if not PUBLIC_BASE_URL.startswith("https://"):
        await update.message.reply_text(
            "Ilova manzili HTTPS emas — Telegram Web App faqat HTTPS orqali ochiladi. "
            "Administratorga murojaat qiling."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🧾 Kassani ochish", web_app=WebAppInfo(url=PUBLIC_BASE_URL))]]
    )
    await update.message.reply_text(
        "MoySklad POS Kassaga xush kelibsiz!\n\n"
        "Kassani ochish uchun pastdagi tugmani bosing, so'ng ismingizni kiriting.",
        reply_markup=keyboard,
    )


async def start_bot() -> None:
    """Botni fon rejimida (long polling) ishga tushiradi.

    Token sozlanmagan bo'lsa (masalan lokal development paytida), botni jim
    o'tkazib yuboradi — ilovaning qolgan qismi bunga bog'liq emas.
    """
    global _application
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN sozlanmagan — Telegram bot ishga tushmaydi.")
        return

    _application = Application.builder().token(BOT_TOKEN).build()
    _application.add_handler(CommandHandler("start", _start_command))

    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling(drop_pending_updates=True)
    me = await _application.bot.get_me()
    logger.info("Telegram bot ishga tushdi: @%s", me.username)


async def stop_bot() -> None:
    if _application is None:
        return
    await _application.updater.stop()
    await _application.stop()
    await _application.shutdown()
