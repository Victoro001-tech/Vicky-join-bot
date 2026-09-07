import os
import threading

import psycopg2
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

TELEGRAM_CHANNEL = "@Vickyupdatemayor"
TELEGRAM_LINK = "https://t.me/Vickyupdatemayor"
WHATSAPP_CHANNEL = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"

REFERRAL_REWARD = 100

db_lock = threading.Lock()

# =========================
# DATABASE
# =========================

def get_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing.")
    return psycopg2.connect(DATABASE_URL)


def init_database():
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, balance BIGINT NOT NULL DEFAULT 0, referrals INTEGER NOT NULL DEFAULT 0, referred_by BIGINT, referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE ) """
                )

                # Safely add columns if an older users table already exists.
                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT """
                )
                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS balance BIGINT NOT NULL DEFAULT 0 """
                )
                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS referrals INTEGER NOT NULL DEFAULT 0 """
                )
                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT """
                )
                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE """
                )

                conn.commit()
        finally:
            conn.close()


def get_user(user_id, username=None):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, users.username) """,
                    (user_id, username),
                )
                conn.commit()
        finally:
            conn.close()


def set_referrer(user_id, referrer_id):
    if user_id == referrer_id:
        return False

    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                # Only the first valid referrer is accepted.
                cursor.execute(
                    """ UPDATE users SET referred_by = %s WHERE user_id = %s AND referred_by IS NULL AND user_id <> %s AND EXISTS ( SELECT 1 FROM users WHERE user_id = %s ) """,
                    (referrer_id, user_id, referrer_id, referrer_id),
                )
                changed = cursor.rowcount > 0
                conn.commit()
                return changed
        finally:
            conn.close()


def reward_referrer(user_id):
    """Reward the referrer once, after the referred user passes the join check."""
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT referred_by, referral_rewarded FROM users WHERE user_id = %s FOR UPDATE """,
                    (user_id,),
                )

                row = cursor.fetchone()

                if not row:
                    conn.rollback()
                    return False

                referrer_id, already_rewarded = row

                if referrer_id is None or already_rewarded:
                    conn.rollback()
                    return False

                cursor.execute(
                    """ UPDATE users SET balance = balance + %s, referrals = referrals + 1 WHERE user_id = %s """,
                    (REFERRAL_REWARD, referrer_id),
                )

                if cursor.rowcount != 1:
                    conn.rollback()
                    return False

                cursor.execute(
                    """ UPDATE users SET referral_rewarded = TRUE WHERE user_id = %s """,
                    (user_id,),
                )

                conn.commit()
                return True
        finally:
            conn.close()


def get_stats(user_id):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT balance, referrals FROM users WHERE user_id = %s """,
                    (user_id,),
                )
                row = cursor.fetchone()
                return row if row else (0, 0)
        finally:
            conn.close()


# =========================
# FLASK SERVER
# =========================

app = Flask(__name__)


@app.route("/")
def home():
    return "Vicky Join Bot is running!"


def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# =========================
# TELEGRAM BOT
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username)

    # A Telegram referral link looks like /start REFERRER_ID.
    if context.args:
        try:
            referrer_id = int(context.args[0])
            set_referrer(user.id, referrer_id)
        except (ValueError, TypeError):
            pass

    await show_join_page(update, context)


async def show_join_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "📢 Join Telegram Channel",
                url=TELEGRAM_LINK,
            )
        ],
        [
            InlineKeyboardButton(
                "🟢 Follow WhatsApp Channel",
                url=WHATSAPP_CHANNEL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ I've Joined — Check",
                callback_data="check",
            )
        ],
    ]

    text = (
        "🔒 *ACCESS LOCKED*\n\n"
        "To use this bot, please complete the requirements below:\n\n"
        "📢 Join our Telegram Channel\n"
        "🟢 Follow our WhatsApp Channel\n\n"
        "After completing both, tap *I've Joined — Check*."
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    try:
        member = await context.bot.get_chat_member(
            chat_id=TELEGRAM_CHANNEL,
            user_id=user_id,
        )
        telegram_joined = member.status in [
            "member",
            "administrator",
            "creator",
        ]
    except Exception:
        telegram_joined = False

    if telegram_joined:
        # The referral becomes successful only after the referred user
        # passes the Telegram join requirement.
        rewarded = reward_referrer(user_id)

        keyboard = [
            [
                InlineKeyboardButton(
                    "🚀 Continue",
                    callback_data="continue",
                )
            ]
        ]

        message = (
            "✅ *Telegram Channel:* Joined\n"
            "🟢 *WhatsApp Channel:* Completed\n\n"
            "🎉 Your requirements are complete!\n\n"
            "Tap *Continue* to access the bot."
        )

        if rewarded:
            message += (
                f"\n\n🎁 Your referrer has earned "
                f"*₦{REFERRAL_REWARD}*."
            )

        await query.message.edit_text(
            message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Telegram Channel",
                    url=TELEGRAM_LINK,
                )
            ],
            [
                InlineKeyboardButton(
                    "🟢 Follow WhatsApp Channel",
                    url=WHATSAPP_CHANNEL,
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Check Again",
                    callback_data="check",
                )
            ],
        ]

        await query.message.edit_text(
            "❌ You haven't joined the Telegram channel yet.\n\n"
            "Please join the channel and then tap *Check Again*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


async def continue_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "👥 My Referrals",
                callback_data="referrals",
            ),
            InlineKeyboardButton(
                "💰 My Balance",
                callback_data="balance",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔗 My Referral Link",
                callback_data="link",
            )
        ],
    ]

    await query.message.edit_text(
        "🎉 *Welcome!*\n\n"
        "Choose an option below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    balance, count = get_stats(query.from_user.id)

    await query.message.edit_text(
        "👥 *My Referrals*\n\n"
        f"Successful referrals: *{count}*\n"
        f"Earned: *₦{count * REFERRAL_REWARD:,}*\n"
        f"Current balance: *₦{balance:,}*",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="continue",
                    )
                ]
            ]
        ),
        parse_mode="Markdown",
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    bal, count = get_stats(query.from_user.id)

    await query.message.edit_text(
        "💰 *Your Balance*\n\n"
        f"Balance: *₦{bal:,}*\n"
        f"Referrals: *{count}*",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="continue",
                    )
                ]
            ]
        ),
        parse_mode="Markdown",
    )


async def referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={query.from_user.id}"

    await query.message.edit_text(
        "🔗 *Your Referral Link*\n\n"
        "Share this link with your friends.\n\n"
        f"`{link}`\n\n"
        f"💰 You earn *₦{REFERRAL_REWARD}* "
        "for every successful referral.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="continue",
                    )
                ]
            ]
        ),
        parse_mode="Markdown",
    )


# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing.")

    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing.")

    init_database()

    threading.Thread(
        target=run_server,
        daemon=True,
    ).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CallbackQueryHandler(
            check_membership,
            pattern="^check$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            continue_bot,
            pattern="^continue$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            referrals,
            pattern="^referrals$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            balance,
            pattern="^balance$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            referral_link,
            pattern="^link$",
        )
    )

    print("Vicky Join Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()
