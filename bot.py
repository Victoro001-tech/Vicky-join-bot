import os
import sqlite3
import threading

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

TELEGRAM_CHANNEL = "@Vickyupdatemayor"
TELEGRAM_LINK = "https://t.me/Vickyupdatemayor"
WHATSAPP_CHANNEL = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"

REFERRAL_REWARD = 100
MINIMUM_WITHDRAWAL = 700

# =========================
# DATABASE
# =========================

conn = sqlite3.connect("bot.db", check_same_thread=False)
db_lock = threading.Lock()

with db_lock:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            referred_by INTEGER,
            referral_rewarded INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def get_user(user_id, username=None):
    with db_lock:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()

        return row


def set_referrer(user_id, referrer_id):
    if user_id == referrer_id:
        return False

    with db_lock:
        user = conn.execute(
            "SELECT referred_by FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not user:
            return False

        if user[0] is not None:
            return False

        referrer = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (referrer_id,)
        ).fetchone()

        if not referrer:
            return False

        conn.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ?",
            (referrer_id, user_id)
        )
        conn.commit()

        return True


def reward_referrer(user_id):
    with db_lock:
        row = conn.execute(
            "SELECT referred_by, referral_rewarded FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        referrer_id, rewarded = row

        if referrer_id is None or rewarded:
            return False

        conn.execute(
            """
            UPDATE users
            SET balance = balance + ?,
                referrals = referrals + 1
            WHERE user_id = ?
            """,
            (REFERRAL_REWARD, referrer_id)
        )

        conn.execute(
            "UPDATE users SET referral_rewarded = 1 WHERE user_id = ?",
            (user_id,)
        )

        conn.commit()

        return True


def get_stats(user_id):
    with db_lock:
        row = conn.execute(
            "SELECT balance, referrals FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            return row

        return 0, 0


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
# JOIN CHECK
# =========================

async def is_telegram_member(bot, user_id):
    try:
        member = await bot.get_chat_member(
            chat_id=TELEGRAM_CHANNEL,
            user_id=user_id
        )

        return member.status in [
            "member",
            "administrator",
            "creator"
        ]

    except Exception:
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    get_user(user.id, user.username)

    # Check referral parameter
    if context.args:
        try:
            referrer_id = int(context.args[0])

            if referrer_id != user.id:
                set_referrer(user.id, referrer_id)

        except ValueError:
            pass

    await show_join_page(update, context)


async def show_join_page(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "📢 Join Telegram Channel",
                url=TELEGRAM_LINK
            )
        ],
        [
            InlineKeyboardButton(
                "🟢 Follow WhatsApp Channel",
                url=WHATSAPP_CHANNEL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ I've Joined — Check",
                callback_data="check"
            )
        ]
    ]

    text = (
        "🔒 *ACCESS LOCKED*\n\n"
        "To use this bot, please complete the requirements:\n\n"
        "📢 Join our Telegram Channel\n"
        "🟢 Follow our WhatsApp Channel\n\n"
        "After completing them, tap *I've Joined — Check*."
    )

    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=markup,
            parse_mode="Markdown"
        )


# =========================
# CHECK REQUIREMENTS
# =========================

async def check_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    joined = await is_telegram_member(
        context.bot,
        user_id
    )

    if not joined:

        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Telegram Channel",
                    url=TELEGRAM_LINK
                )
            ],
            [
                InlineKeyboardButton(
                    "🟢 Follow WhatsApp Channel",
                    url=WHATSAPP_CHANNEL
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Check Again",
                    callback_data="check"
                )
            ]
        ]

        await query.message.edit_text(
            "❌ *Telegram requirement not completed.*\n\n"
            "Please join our Telegram channel first, "
            "then tap *Check Again*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        return

    # Successful Telegram verification
    reward_referrer(user_id)

    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Continue",
                callback_data="continue"
            )
        ]
    ]

    await query.message.edit_text(
        "🎉 *Welcome!*\n\n"
        "✅ Telegram Channel: Joined\n"
        "🟢 WhatsApp Channel: Completed\n\n"
        "Your requirements are complete!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# =========================
# MAIN MENU
# =========================

async def continue_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                "👥 My Referrals",
                callback_data="referrals"
            ),
            InlineKeyboardButton(
                "💰 My Balance",
                callback_data="balance"
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 My Referral Link",
                callback_data="link"
            )
        ],
        [
            InlineKeyboardButton(
                "💸 Withdraw",
                callback_data="withdraw"
            )
        ]
    ]

    await query.message.edit_text(
        "🎉 *Vicky Updates*\n\n"
        "Welcome! Choose an option below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# =========================
# REFERRALS
# =========================

async def referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    balance, count = get_stats(
        query.from_user.id
    )

    await query.message.edit_text(
        f"👥 *My Referrals*\n\n"
        f"Successful referrals: *{count}*\n"
        f"Earned: *₦{count * REFERRAL_REWARD:,}*\n"
        f"Current balance: *₦{balance:,}*",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="continue"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================
# BALANCE
# =========================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    bal, count = get_stats(
        query.from_user.id
    )

    await query.message.edit_text(
        f"💰 *Your Balance*\n\n"
        f"Balance: *₦{bal:,}*\n"
        f"Referrals: *{count}*\n\n"
        f"Minimum withdrawal: *₦{MINIMUM_WITHDRAWAL:,}*",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="continue"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================
# REFERRAL LINK
# =========================

async def referral_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="continue"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================
# WITHDRAW
# =========================

async def withdraw(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query
    await query.answer()

    bal, count = get_stats(
        query.from_user.id
    )

    if bal < MINIMUM_WITHDRAWAL:

        remaining = MINIMUM_WITHDRAWAL - bal

        await query.message.edit_text(
            "💸 *Withdrawal*\n\n"
            f"Your balance: *₦{bal:,}*\n"
            f"Minimum withdrawal: *₦{MINIMUM_WITHDRAWAL:,}*\n\n"
            f"You need *₦{remaining:,}* more "
            "before you can withdraw.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back",
                        callback_data="continue"
                    )
                ]
            ]),
            parse_mode="Markdown"
        )

        return

    await query.message.edit_text(
        "💸 *Withdrawal Available*\n\n"
        f"Your balance: *₦{bal:,}*\n\n"
        "Please contact the administrator to request "
        "your withdrawal.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔙 Back",
                    callback_data="continue"
                )
            ]
        ]),
        parse_mode="Markdown"
    )


# =========================
# MAIN
# =========================

def main():

    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN environment variable is missing."
        )

    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CallbackQueryHandler(
            check_membership,
            pattern="^check$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            continue_bot,
            pattern="^continue$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            referrals,
            pattern="^referrals$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            balance,
            pattern="^balance$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            referral_link,
            pattern="^link$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            withdraw,
            pattern="^withdraw$"
        )
    )

    print("Vicky Join Bot started!")

    application.run_polling()


if __name__ == "__main__":
    main()
