import os
import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, jsonify
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============================================================
# VICKY JOIN BOT - SINGLE FILE VERSION
# ============================================================
# Required Render environment variables:
#
# BOT_TOKEN = Telegram bot token
# DATABASE_URL = PostgreSQL connection string
# ADMIN_IDS = Telegram admin IDs, comma separated
# CHANNEL_ID = Telegram channel/group ID to verify membership
# CHANNEL_LINK = Telegram join link
#
# Optional:
# WHATSAPP_LINK = WhatsApp/community link
# REFERRAL_REWARD = 100
# MIN_WITHDRAWAL = 700
# PORT = 10000
#
# The bot uses Telegram polling + a Flask health server so it works
# correctly on Render. The withdrawal flow is fully stateful and
# survives restarts because requests are stored in PostgreSQL.
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("vicky-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "").strip()
WHATSAPP_LINK = os.getenv("WHATSAPP_LINK", "").strip()

try:
    REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD", "100"))
except ValueError:
    REFERRAL_REWARD = 100

try:
    MIN_WITHDRAWAL = int(os.getenv("MIN_WITHDRAWAL", "700"))
except ValueError:
    MIN_WITHDRAWAL = 700

try:
    PORT = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

ADMIN_IDS = set()
for item in os.getenv("ADMIN_IDS", "").split(","):
    item = item.strip()
    if item:
        try:
            ADMIN_IDS.add(int(item))
        except ValueError:
            logger.warning("Invalid ADMIN_IDS value ignored: %s", item)

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW else None
except ValueError:
    CHANNEL_ID = CHANNEL_ID_RAW or None

# Withdrawal conversation states
WITHDRAW_AMOUNT, WITHDRAW_METHOD, WITHDRAW_DETAILS = range(3)

app = Flask(__name__)


@app.get("/")
def home():
    return "Vicky Join Bot is running."


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "vicky-join-bot"})


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_database():
    """Create/upgrade all tables without deleting existing data."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(""" CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, balance BIGINT NOT NULL DEFAULT 0, referrals INTEGER NOT NULL DEFAULT 0, referred_by BIGINT, referral_rewarded INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW() ) """)

            cur.execute(""" CREATE TABLE IF NOT EXISTS withdrawals ( id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount BIGINT NOT NULL, method TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', admin_id BIGINT, admin_note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), processed_at TIMESTAMPTZ ) """)

            cur.execute(""" CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status) """)

            cur.execute(""" CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id) """)

            # Safely add columns to databases created by older versions.
            cur.execute(""" ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT """)
            cur.execute(""" ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW() """)
            cur.execute(""" ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded INTEGER NOT NULL DEFAULT 0 """)

        conn.commit()
        logger.info("Database initialized successfully.")
    finally:
        conn.close()


def get_user(user_id):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM users WHERE user_id = %s",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def upsert_user(tg_user, referred_by=None):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM users WHERE user_id = %s FOR UPDATE",
                (tg_user.id,),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(""" UPDATE users SET username = %s, first_name = %s, updated_at = NOW() WHERE user_id = %s """, (tg_user.username, tg_user.first_name, tg_user.id))
                conn.commit()
                return existing

            # Never allow self-referral.
            if referred_by == tg_user.id:
                referred_by = None

            cur.execute(""" INSERT INTO users (user_id, username, first_name, referred_by) VALUES (%s, %s, %s, %s) RETURNING * """, (
                tg_user.id,
                tg_user.username,
                tg_user.first_name,
                referred_by,
            ))
            new_user = cur.fetchone()

            # Reward the inviter once, only after the new account is created.
            if referred_by:
                cur.execute(""" UPDATE users SET balance = balance + %s, referrals = referrals + 1, updated_at = NOW() WHERE user_id = %s """, (REFERRAL_REWARD, referred_by))

                if cur.rowcount:
                    cur.execute(""" UPDATE users SET referral_rewarded = 1 WHERE user_id = %s """, (tg_user.id,))

            conn.commit()
            return new_user
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def is_channel_member(context, user_id):
    """Return True if the user has joined the configured Telegram channel/group."""
    if not CHANNEL_ID:
        return True

    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception as exc:
        logger.warning("Membership check failed for %s: %s", user_id, exc)
        # Do not block the whole bot if membership checking is temporarily
        # unavailable. The Join button remains available.
        return False


def main_keyboard():
    rows = [
        [
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
            InlineKeyboardButton("👥 Referrals", callback_data="referrals"),
        ],
        [
            InlineKeyboardButton("🔗 My Link", callback_data="my_link"),
            InlineKeyboardButton("💸 Withdraw", callback_data="withdraw"),
        ],
    ]
    if CHANNEL_LINK or WHATSAPP_LINK:
        join_row = []
        if CHANNEL_LINK:
            join_row.append(InlineKeyboardButton("📢 Join Telegram", url=CHANNEL_LINK))
        if WHATSAPP_LINK:
            join_row.append(InlineKeyboardButton("🟢 WhatsApp", url=WHATSAPP_LINK))
        rows.append(join_row)
    return InlineKeyboardMarkup(rows)


def join_keyboard():
    rows = []
    if CHANNEL_LINK:
        rows.append([InlineKeyboardButton("📢 Join Telegram", url=CHANNEL_LINK)])
    if WHATSAPP_LINK:
        rows.append([InlineKeyboardButton("🟢 Join WhatsApp", url=WHATSAPP_LINK)])
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


def cancel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_withdraw")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    referred_by = None
    if context.args:
        arg = context.args[0].strip()
        if arg.startswith("ref_"):
            try:
                referred_by = int(arg[4:])
            except ValueError:
                referred_by = None

    try:
        user_row = upsert_user(user, referred_by)
    except Exception:
        logger.exception("Could not create/update user")
        await update.message.reply_text(
            "⚠️ Something went wrong while loading your account. Please try again."
        )
        return

    joined = await is_channel_member(context, user.id)

    if not joined and CHANNEL_ID:
        await update.message.reply_text(
            "👋 Welcome!\n\n"
            "Please join the required Telegram channel first, then tap "
            "“I've Joined”.",
            reply_markup=join_keyboard(),
        )
        return

    await update.message.reply_text(
        "🎉 Welcome to Vicky Join Bot!\n\n"
        "Use the buttons below to check your balance, referrals and withdraw.",
        reply_markup=main_keyboard(),
    )


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await is_channel_member(context, query.from_user.id):
        await query.edit_message_text(
            "✅ Membership confirmed!\n\n"
            "Your account is ready. Use the menu below.",
            reply_markup=main_keyboard(),
        )
    else:
        await query.answer(
            "You haven't joined the required Telegram channel yet.",
            show_alert=True,
        )


async def balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = get_user(query.from_user.id)
    balance = int(user["balance"]) if user else 0

    await query.edit_message_text(
        f"💰 <b>Your Balance</b>\n\n"
        f"₦{balance:,}\n\n"
        f"Minimum withdrawal: ₦{MIN_WITHDRAWAL:,}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def referrals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = get_user(query.from_user.id)
    referrals = int(user["referrals"]) if user else 0

    await query.edit_message_text(
        f"👥 <b>Referrals</b>\n\n"
        f"Total referrals: {referrals}\n"
        f"Reward per successful referral: ₦{REFERRAL_REWARD:,}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def my_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{query.from_user.id}"

    await query.edit_message_text(
        "🔗 <b>Your referral link</b>\n\n"
        f"{link}\n\n"
        f"Earn ₦{REFERRAL_REWARD:,} for each successful referral.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """This is deliberately a dedicated callback handler for the Withdraw button."""
    query = update.callback_query
    await query.answer()

    user = get_user(query.from_user.id)
    balance = int(user["balance"]) if user else 0

    if balance < MIN_WITHDRAWAL:
        await query.edit_message_text(
            f"💸 <b>Withdraw</b>\n\n"
            f"Your balance: ₦{balance:,}\n"
            f"Minimum withdrawal: ₦{MIN_WITHDRAWAL:,}\n\n"
            f"You need ₦{MIN_WITHDRAWAL - balance:,} more before you can withdraw.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return ConversationHandler.END

    context.user_data["withdraw_balance_at_start"] = balance

    await query.edit_message_text(
        f"💸 <b>Withdrawal</b>\n\n"
        f"Available balance: ₦{balance:,}\n"
        f"Minimum: ₦{MIN_WITHDRAWAL:,}\n\n"
        "Enter the amount you want to withdraw.\n"
        "Example: <code>1000</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )

    return WITHDRAW_AMOUNT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").replace(",", "").strip()

    try:
        amount = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid whole number, for example <b>1000</b>.",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )
        return WITHDRAW_AMOUNT

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is ₦{MIN_WITHDRAWAL:,}.",
            reply_markup=cancel_keyboard(),
        )
        return WITHDRAW_AMOUNT

    user = get_user(update.effective_user.id)
    balance = int(user["balance"]) if user else 0

    if amount > balance:
        await update.message.reply_text(
            f"❌ You only have ₦{balance:,} available.",
            reply_markup=cancel_keyboard(),
        )
        return WITHDRAW_AMOUNT

    # Do not deduct yet. The amount is reserved atomically when the request
    # is created, preventing double-spending while it is pending.
    context.user_data["withdraw_amount"] = amount

    await update.message.reply_text(
        "💳 <b>Choose your withdrawal method</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🏦 Bank", callback_data="method_bank"),
                InlineKeyboardButton("📱 Other", callback_data="method_other"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_withdraw")],
        ]),
    )
    return WITHDRAW_METHOD


async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_withdraw":
        return await cancel_withdraw(update, context)

    method = "Bank" if query.data == "method_bank" else "Other"
    context.user_data["withdraw_method"] = method

    await query.edit_message_text(
        f"💳 Method: <b>{method}</b>\n\n"
        "Send your payment details in one message.\n\n"
        "For Bank, send:\n"
        "<code>Bank name | Account name | Account number</code>",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )
    return WITHDRAW_DETAILS


async def withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    details = (update.message.text or "").strip()

    if len(details) < 5:
        await update.message.reply_text(
            "❌ Please provide valid payment details.",
            reply_markup=cancel_keyboard(),
        )
        return WITHDRAW_DETAILS

    amount = int(context.user_data.get("withdraw_amount", 0))
    method = context.user_data.get("withdraw_method", "Other")

    if amount <= 0:
        await update.message.reply_text(
            "⚠️ Your withdrawal session expired. Tap Withdraw again.",
            reply_markup=main_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Lock the user's row so two simultaneous withdrawals cannot
            # spend the same balance.
            cur.execute(
                "SELECT balance FROM users WHERE user_id = %s FOR UPDATE",
                (user.id,),
            )
            row = cur.fetchone()

            if not row or int(row["balance"]) < amount:
                conn.rollback()
                await update.message.reply_text(
                    "❌ Your balance is no longer enough for this withdrawal.",
                    reply_markup=main_keyboard(),
                )
                context.user_data.clear()
                return ConversationHandler.END

            # Reserve the money immediately.
            cur.execute(""" UPDATE users SET balance = balance - %s, updated_at = NOW() WHERE user_id = %s """, (amount, user.id))

            cur.execute(""" INSERT INTO withdrawals (user_id, amount, method, details, status) VALUES (%s, %s, %s, %s, 'pending') RETURNING id """, (user.id, amount, method, details))

            withdrawal = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Failed to create withdrawal")
        await update.message.reply_text(
            "⚠️ I couldn't create the withdrawal right now. Your balance was not changed.",
            reply_markup=main_keyboard(),
        )
        context.user_data.clear()
        return ConversationHandler.END
    finally:
        conn.close()

    withdrawal_id = withdrawal["id"]

    # Notify every configured admin.
    admin_text = (
        "🔔 <b>NEW WITHDRAWAL REQUEST</b>\n\n"
        f"ID: <code>#{withdrawal_id}</code>\n"
        f"User: <code>{user.id}</code>\n"
        f"Username: @{user.username if user.username else 'none'}\n"
        f"Name: {user.full_name}\n"
        f"Amount: <b>₦{amount:,}</b>\n"
        f"Method: {method}\n"
        f"Details: <code>{details}</code>\n\n"
        "Choose an action:"
    )

    admin_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Approve",
                callback_data=f"wd_approve_{withdrawal_id}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"wd_reject_{withdrawal_id}",
            ),
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                parse_mode="HTML",
                reply_markup=admin_keyboard,
            )
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)

    await update.message.reply_text(
        f"✅ <b>Withdrawal request submitted!</b>\n\n"
        f"Request ID: <code>#{withdrawal_id}</code>\n"
        f"Amount: ₦{amount:,}\n"
        f"Method: {method}\n\n"
        "Your balance has been reserved while the request is being processed.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "❌ Withdrawal cancelled.",
            reply_markup=main_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ Withdrawal cancelled.",
            reply_markup=main_keyboard(),
        )

    return ConversationHandler.END


async def admin_withdrawal_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()

    parts = query.data.split("_")
    if len(parts) != 3:
        return

    action = parts[1]
    try:
        withdrawal_id = int(parts[2])
    except ValueError:
        return

    conn = get_connection()
    withdrawal = None

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Loc
