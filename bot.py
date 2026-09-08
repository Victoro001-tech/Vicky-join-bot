import os
import logging
import threading
from flask import Flask
import psycopg2
from psycopg2 import pool
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# ============================================================
# Vicky Join Bot - Render-ready version
# Environment variables required on Render:
# BOT_TOKEN
# DATABASE_URL
# Optional:
# PORT
# ADMIN_ID
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing from Render Environment Variables.")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing from Render Environment Variables.")

db_pool = None


# -------------------------
# Database
# -------------------------
def init_database():
    global db_pool

    db_pool = pool.SimpleConnectionPool(
        1,
        5,
        dsn=DATABASE_URL,
        sslmode="require",
    )

    conn = db_pool.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """ CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, referrals INTEGER DEFAULT 0, referred_by BIGINT, referral_rewarded INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ) """
            )
            conn.commit()
    finally:
        db_pool.putconn(conn)

    logger.info("Database initialized successfully.")


def db_execute(query, params=(), fetchone=False, fetchall=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            result = None

            if fetchone:
                result = cursor.fetchone()
            elif fetchall:
                result = cursor.fetchall()

            conn.commit()
            return result
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)


def register_user(user_id, username, referred_by=None):
    existing = db_execute(
        "SELECT user_id FROM users WHERE user_id = %s",
        (user_id,),
        fetchone=True,
    )

    if existing:
        db_execute(
            "UPDATE users SET username = %s WHERE user_id = %s",
            (username, user_id),
        )
        return False

    valid_referrer = None
    if referred_by and referred_by != user_id:
        referrer = db_execute(
            "SELECT user_id FROM users WHERE user_id = %s",
            (referred_by,),
            fetchone=True,
        )
        if referrer:
            valid_referrer = referred_by

    db_execute(
        """ INSERT INTO users (user_id, username, balance, referrals, referred_by, referral_rewarded) VALUES (%s, %s, 0, 0, %s, 0) """,
        (user_id, username, valid_referrer),
    )

    # Reward the referrer only once, when the referred user is created.
    if valid_referrer:
        db_execute(
            """ UPDATE users SET referrals = referrals + 1, balance = balance + 1 WHERE user_id = %s """,
            (valid_referrer,),
        )

    return True


def get_user(user_id):
    return db_execute(
        """ SELECT user_id, username, balance, referrals, referred_by FROM users WHERE user_id = %s """,
        (user_id,),
        fetchone=True,
    )


# -------------------------
# Telegram handlers
# -------------------------
WELCOME_TEXT = (
    "👋 <b>Welcome to Vicky Join Bot!</b>\n\n"
    "You're successfully connected.\n\n"
    "Use the buttons below to check your balance, referrals, "
    "or get your personal referral link."
)

HELP_TEXT = (
    "🤖 <b>Bot Commands</b>\n\n"
    "/start - Open the main menu\n"
    "/balance - Check your balance\n"
    "/referrals - Check your referrals\n"
    "/invite - Get your referral link\n"
    "/help - Show this help message"
)


def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💰 Balance", callback_data="balance"),
                InlineKeyboardButton("👥 Referrals", callback_data="referrals"),
            ],
            [
                InlineKeyboardButton("🔗 Invite Friends", callback_data="invite"),
            ],
            [
                InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
            ],
            [
                InlineKeyboardButton("ℹ️ Help", callback_data="help"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    referred_by = None
    if context.args:
        referral_code = context.args[0].strip()
        if referral_code.startswith("ref_"):
            try:
                referred_by = int(referral_code[4:])
            except ValueError:
                referred_by = None
        else:
            try:
                referred_by = int(referral_code)
            except ValueError:
                referred_by = None

    register_user(
        user.id,
        user.username or "",
        referred_by,
    )

    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)

    if not row:
        register_user(user.id, user.username or "")
        row = get_user(user.id)

    await update.message.reply_text(
        f"💰 <b>Your Balance</b>\n\n"
        f"Balance: <b>{row[2]}</b>\n"
        f"Referrals: <b>{row[3]}</b>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)

    if not row:
        register_user(user.id, user.username or "")
        row = get_user(user.id)

    await update.message.reply_text(
        f"👥 <b>Your Referrals</b>\n\n"
        f"You have referred <b>{row[3]}</b> user(s).\n"
        f"Your balance is <b>{row[2]}</b>.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=ref_{user.id}"

    await update.message.reply_text(
        "🔗 <b>Your Referral Link</b>\n\n"
        f"<code>{link}</code>\n\n"
        "Send this link to your friends.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_TEXT,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    row = get_user(user_id)

    if not row:
        register_user(user_id, query.from_user.username or "")
        row = get_user(user_id)

    if query.data == "balance":
        text = (
            f"💰 <b>Your Balance</b>\n\n"
            f"Balance: <b>{row[2]}</b>\n"
            f"Referrals: <b>{row[3]}</b>"
        )

    elif query.data == "referrals":
        text = (
            f"👥 <b>Your Referrals</b>\n\n"
            f"You have referred <b>{row[3]}</b> user(s)."
        )

    elif query.data == "invite":
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=ref_{user_id}"
        text = (
            "🔗 <b>Your Referral Link</b>\n\n"
            f"<code>{link}</code>\n\n"
            "Send this link to your friends."
        )

    else:
        text = HELP_TEXT

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# -------------------------
# Render health server
# -------------------------
flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return "Vicky Join Bot is running ✅", 200


@flask_app.get("/health")
def health_check():
    return "OK", 200


def run_web_server():
    flask_app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False,
    )


# -------------------------
# Main
# -------------------------
def main():
    logger.info("Starting Vicky Join Bot...")

    init_database()

    web_thread = threading.Thread(
        target=run_web_server,
        daemon=True,
    )
    web_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("referrals", referrals))
    application.add_handler(CommandHandler("invite", invite))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot is now running.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
