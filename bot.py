import os
import threading

import psycopg2
from flask import Flask
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

ADMIN_ID = 6225743234

TELEGRAM_CHANNEL = "@Vickyupdatemayor"
TELEGRAM_LINK = "https://t.me/Vickyupdatemayor"
WHATSAPP_CHANNEL = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"

REFERRAL_REWARD = 100
MIN_WITHDRAWAL = 500

# Prevent two threads from using the same database logic at once.
db_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

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
                    """ CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, balance BIGINT NOT NULL DEFAULT 0, referrals INTEGER NOT NULL DEFAULT 0, referred_by BIGINT, referral_rewarded INTEGER NOT NULL DEFAULT 0 ) """
                )

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

                # Convert the old BOOLEAN referral_rewarded column, if present.
                cursor.execute(
                    """ DO $$ BEGIN IF EXISTS ( SELECT 1 FROM information_schema.columns WHERE table_name = 'users' AND column_name = 'referral_rewarded' AND data_type = 'boolean' ) THEN ALTER TABLE users ALTER COLUMN referral_rewarded TYPE INTEGER USING CASE WHEN referral_rewarded THEN 1 ELSE 0 END; END IF; END $$ """
                )

                cursor.execute(
                    """ ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded INTEGER NOT NULL DEFAULT 0 """
                )

                cursor.execute(
                    """ CREATE TABLE IF NOT EXISTS withdrawals ( id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount BIGINT NOT NULL, bank_name TEXT NOT NULL, account_number TEXT NOT NULL, account_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ) """
                )

                cursor.execute(
                    """ CREATE INDEX IF NOT EXISTS withdrawals_status_idx ON withdrawals(status) """
                )

            conn.commit()
            print("Database initialized successfully.")
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
                cursor.execute(
                    """ UPDATE users SET referred_by = %s WHERE user_id = %s AND referred_by IS NULL AND user_id <> %s AND EXISTS (SELECT 1 FROM users WHERE user_id = %s) """,
                    (referrer_id, user_id, referrer_id, referrer_id),
                )
                changed = cursor.rowcount > 0
            conn.commit()
            return changed
        finally:
            conn.close()


def reward_referrer(user_id):
    """Reward the referrer exactly once after membership is verified."""
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
                if referrer_id is None or int(already_rewarded or 0) == 1:
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
                    """ UPDATE users SET referral_rewarded = 1 WHERE user_id = %s """,
                    (user_id,),
                )

            conn.commit()
            print(
                f"Referral rewarded: user={user_id}, "
                f"referrer={referrer_id}, amount=NGN{REFERRAL_REWARD}"
            )
            return True
        finally:
            conn.close()


def get_stats(user_id):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT balance, referrals FROM users WHERE user_id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
                return row if row else (0, 0)
        finally:
            conn.close()


def create_withdrawal(user_id, amount, bank_name, account_number, account_name):
    """Deduct the amount and create a pending withdrawal atomically."""
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT balance FROM users WHERE user_id = %s FOR UPDATE",
                    (user_id,),
                )
                row = cursor.fetchone()
                if not row:
                    conn.rollback()
                    return None, "user_not_found"

                balance = int(row[0])
                if balance < amount:
                    conn.rollback()
                    return None, "insufficient_balance"

                cursor.execute(
                    "UPDATE users SET balance = balance - %s WHERE user_id = %s",
                    (amount, user_id),
                )
                cursor.execute(
                    """ INSERT INTO withdrawals (user_id, amount, bank_name, account_number, account_name, status) VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id """,
                    (user_id, amount, bank_name, account_number, account_name),
                )
                withdrawal_id = cursor.fetchone()[0]

            conn.commit()
            return withdrawal_id, None
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_withdrawal(withdrawal_id):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT id, user_id, amount, bank_name, account_number, account_name, status, created_at FROM withdrawals WHERE id = %s """,
                    (withdrawal_id,),
                )
                return cursor.fetchone()
        finally:
            conn.close()


def get_pending_withdrawals(limit=20):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT id, user_id, amount, bank_name, account_number, account_name, status, created_at FROM withdrawals WHERE status = 'pending' ORDER BY created_at ASC LIMIT %s """,
                    (limit,),
                )
                return cursor.fetchall()
        finally:
            conn.close()


def approve_withdrawal(withdrawal_id):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ UPDATE withdrawals SET status = 'approved' WHERE id = %s AND status = 'pending' RETURNING user_id, amount """,
                    (withdrawal_id,),
                )
                row = cursor.fetchone()
                if not row:
                    conn.rollback()
                    return None
            conn.commit()
            return row
        finally:
            conn.close()


def reject_withdrawal(withdrawal_id):
    """Reject a pending withdrawal and return its reserved amount to the user."""
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT user_id, amount FROM withdrawals WHERE id = %s AND status = 'pending' FOR UPDATE """,
                    (withdrawal_id,),
                )
                row = cursor.fetchone()
                if not row:
                    conn.rollback()
                    return None

                user_id, amount = row

                cursor.execute(
                    "UPDATE users SET balance = balance + %s WHERE user_id = %s",
                    (amount, user_id),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None

                cursor.execute(
                    """ UPDATE withdrawals SET status = 'rejected' WHERE id = %s AND status = 'pending' """,
                    (withdrawal_id,),
                )

            conn.commit()
            return user_id, amount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_admin_stats():
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM users")
                users, total_balance = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'"
                )
                pending = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM withdrawals WHERE status = 'approved'"
                )
                approved = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT COUNT(*) FROM withdrawals WHERE status = 'rejected'"
                )
                rejected = cursor.fetchone()[0]
                return users, total_balance, pending, approved, rejected
        finally:
            conn.close()


def get_users(limit=20):
    with db_lock:
        conn = get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """ SELECT user_id, username, balance, referrals FROM users ORDER BY user_id DESC LIMIT %s """,
                    (limit,),
                )
                return cursor.fetchall()
        finally:
            conn.close()


# ============================================================
# FLASK SERVER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Vicky Join Bot is running!"


def run_server():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)


# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


def main_menu_markup():
    keyboard = [
        [
            InlineKeyboardButton("👥 My Referrals", callback_data="referrals"),
            InlineKeyboardButton("💰 My Balance", callback_data="balance"),
        ],
        [InlineKeyboardButton("🔗 My Referral Link", callback_data="link")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================
# START / JOIN CHECK
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            set_referrer(user.id, referrer_id)
        except (ValueError, TypeError):
            pass

    await show_join_page(update, context)


async def show_join_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📢 Join Telegram Channel", url=TELEGRAM_LINK)],
        [InlineKeyboardButton("🟢 Follow WhatsApp Channel", url=WHATSAPP_CHANNEL)],
        [InlineKeyboardButton("✅ I've Joined — Check", callback_data="check")],
    ]

    text = (
        "🔒 *ACCESS LOCKED*\n\n"
        "To use this bot, please complete the requirements below:\n\n"
        "📢 Join our Telegram Channel\n"
        "🟢 Follow our WhatsApp Channel\n\n"
        "After completing both, tap *I've Joined — Check*."
    )
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    telegram_joined = False
    try:
        member = await context.bot.get_chat_member(
            chat_id=TELEGRAM_CHANNEL,
            user_id=user_id,
        )
        print(f"Membership check: user={user_id}, status={member.status}")
        telegram_joined = member.status in ("member", "administrator", "creator")
    except Exception as exc:
        print(f"Telegram membership check error: {exc}")

    if not telegram_joined:
        keyboard = [
            [InlineKeyboardButton("📢 Join Telegram Channel", url=TELEGRAM_LINK)],
            [InlineKeyboardButton("🟢 Follow WhatsApp Channel", url=WHATSAPP_CHANNEL)],
            [InlineKeyboardButton("🔄 Check Again", callback_data="check")],
        ]
        await query.message.edit_text(
            "❌ *Telegram Join Not Detected*\n\n"
            "Please join the Telegram channel first, then tap *Check Again*.\n\n"
            "If you already joined, make sure the bot is an administrator of the channel.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    rewarded = reward_referrer(user_id)
    message = (
        "✅ *Telegram Channel:* Joined\n"
        "🟢 *WhatsApp Channel:* Completed\n\n"
        "🎉 Your requirements are complete!\n\n"
        "Tap *Continue* to access the bot."
    )
    if rewarded:
        message += f"\n\n🎁 Your referrer has earned *₦{REFERRAL_REWARD:,}*."

    await query.message.edit_text(
        message,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 Continue", callback_data="continue")]]
        ),
        parse_mode="Markdown",
    )


# ============================================================
# USER MENU
# ============================================================

async def continue_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "🎉 *Welcome!*\n\nChoose an option below:",
        reply_markup=main_menu_markup(),
        parse_mode="Markdown",
    )


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, count = get_stats(query.from_user.id)
    await query.message.edit_text(
        "👥 *My Referrals*\n\n"
        f"Successful referrals: *{count}*\n"
        f"Earned: *₦{count * REFERRAL_REWARD:,}*\n"
        f"Current balance: *₦{bal:,}*",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Back", callback_data="continue")]]
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
            [[InlineKeyboardButton("🔙 Back", callback_data="continue")]]
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
        f"💰 You earn *₦{REFERRAL_REWARD:,}* for every successful referral.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Back", callback_data="continue")]]
        ),
        parse_mode="Markdown",
    )


# ============================================================
# WITHDRAWAL
# ============================================================

async def withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    bal, _ = get_stats(user_id)

    if bal < MIN_WITHDRAWAL:
        await query.message.edit_text(
            "❌ *Withdrawal Unavailable*\n\n"
            f"Your balance: *₦{bal:,}*\n"
            f"Minimum withdrawal: *₦{MIN_WITHDRAWAL:,}*",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back", callback_data="continue")]]
            ),
            parse_mode="Markdown",
        )
        return

    context.user_data["waiting_for_bank_details"] = True
    await query.message.edit_text(
        "💸 *Withdrawal Request*\n\n"
        f"Available balance: *₦{bal:,}*\n\n"
        "Send your bank details in exactly this format:\n\n"
        "`Bank Name`\n"
        "`Account Number`\n"
        "`Account Name`\n\n"
        "Example:\n"
        "`Access Bank`\n"
        "`0123456789`\n"
        "`John Doe`\n\n"
        "Your balance is reserved only after your details are accepted.",
        parse_mode="Markdown",
    )


async def receive_bank_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_bank_details"):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
                # Convert it safely to INTEGER while preserving
                # existing TRUE/FALSE referral status.
                cursor.execute(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'users'
                            AND column_name = 'referral_rewarded'
                            AND data_type = 'boolean'
                        ) THEN
                            ALTER TABLE users
                            ALTER COLUMN referral_rewarded
                            TYPE INTEGER
                            USING CASE
                                WHEN referral_rewarded
                                THEN 1
                                ELSE 0
                            END;
                        END IF;
                    END
                    $$
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS referral_rewarded
                    INTEGER NOT NULL DEFAULT 0
                    """
                )

                # Withdrawal table.
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS withdrawals (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        amount BIGINT NOT NULL,
                        bank_name TEXT NOT NULL,
                        account_number TEXT NOT NULL,
                        account_name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                conn.commit()

                print("Database initialized successfully.")

        finally:
            conn.close()


def get_user(user_id, username=None):
    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (user_id, username)
                    VALUES (%s, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        username = COALESCE(
                            EXCLUDED.username,
                            users.username
                        )
                    """,
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
                cursor.execute(
                    """
                    UPDATE users
                    SET referred_by = %s
                    WHERE user_id = %s
                    AND referred_by IS NULL
                    AND user_id <> %s
                    AND EXISTS (
                        SELECT 1
                        FROM users
                        WHERE user_id = %s
                    )
                    """,
                    (
                        referrer_id,
                        user_id,
                        referrer_id,
                        referrer_id,
                    ),
                )

                changed = cursor.rowcount > 0

                conn.commit()

                return changed

        finally:
            conn.close()


def reward_referrer(user_id):
    """
    Give ₦100 to the referrer once.
    This happens only after the referred user
    passes the Telegram membership check.
    """

    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT referred_by, referral_rewarded
                    FROM users
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                )

                row = cursor.fetchone()

                if not row:
                    conn.rollback()
                    return False

                referrer_id, already_rewarded = row

                if referrer_id is None:
                    conn.rollback()
                    return False

                if int(already_rewarded or 0) == 1:
                    conn.rollback()
                    return False

                cursor.execute(
                    """
                    UPDATE users
                    SET balance = balance + %s,
                        referrals = referrals + 1
                    WHERE user_id = %s
                    """,
                    (
                        REFERRAL_REWARD,
                        referrer_id,
                    ),
                )

                if cursor.rowcount != 1:
                    conn.rollback()
                    return False

                cursor.execute(
                    """
                    UPDATE users
                    SET referral_rewarded = 1
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )

                conn.commit()

                print(
                    f"Referral rewarded: user={user_id}, "
                    f"referrer={referrer_id}, "
                    f"amount=₦{REFERRAL_REWARD}"
                )

                return True

        finally:
            conn.close()


def get_stats(user_id):
    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT balance, referrals
                    FROM users
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )

                row = cursor.fetchone()

                if row:
                    return row

                return (0, 0)

        finally:
            conn.close()


# ============================================================
# FLASK SERVER
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Vicky Join Bot is running!"


def run_server():
    port = int(
        os.environ.get("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
    )


# ============================================================
# TELEGRAM COMMAND MENU
# ============================================================

async def post_init(application):
    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Start Freecash_bot",
            ),
            BotCommand(
                "admin",
                "Open admin panel",
            ),
            BotCommand(
                "help",
                "Help",
            ),
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    get_user(
        user.id,
        user.username,
    )

    # Referral link:
    # /start REFERRER_ID
    if context.args:

        try:
            referrer_id = int(
                context.args[0]
            )

            set_referrer(
                user.id,
                referrer_id,
            )

        except (
            ValueError,
            TypeError,
        ):
            pass

    await show_join_page(
        update,
        context,
    )


# ============================================================
# JOIN PAGE
# ============================================================

async def show_join_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
        "To use this bot, please complete "
        "the requirements below:\n\n"
        "📢 Join our Telegram Channel\n"
        "🟢 Follow our WhatsApp Channel\n\n"
        "After completing both, tap "
        "*I've Joined — Check*."
    )

    markup = InlineKeyboardMarkup(
        keyboard
    )

    if update.callback_query:

        await update.callback_query.message.edit_text(
            text,
            reply_markup=markup,
            parse_mode="Markdown",
        )

    else:

        await update.message.reply_text(
            text,
            reply_markup=markup,
            parse_mode="Markdown",
        )


# ============================================================
# TELEGRAM MEMBERSHIP CHECK
# ============================================================

async def check_membership(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    telegram_joined = False

    try:

        member = await context.bot.get_chat_member(
            chat_id=TELEGRAM_CHANNEL,
            user_id=user_id,
        )

        print(
            f"Membership check: "
            f"user={user_id}, "
            f"status={member.status}"
        )

        telegram_joined = member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        print(
            "Telegram membership check error:",
            e,
        )

    if not telegram_joined:

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
            "❌ *Telegram Join Not Detected*\n\n"
            "Please join the Telegram channel "
            "first, then tap *Check Again*.\n\n"
            "If you already joined, make sure "
            "the bot is an administrator of "
            "the channel.",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
            parse_mode="Markdown",
        )

        return

    # Successful Telegram membership check.
    rewarded = reward_referrer(
        user_id
    )

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
            f"*₦{REFERRAL_REWARD:,}*."
        )

    await query.message.edit_text(
        message,
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
        parse_mode="Markdown",
    )


# ============================================================
# MAIN USER MENU
# ============================================================

async def continue_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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
        [
            InlineKeyboardButton(
                "💸 Withdraw",
                callback_data="withdraw",
            )
        ],
    ]

    await query.message.edit_text(
        "🎉 *Welcome!*\n\n"
        "Choose an option below:",
        reply_markup=InlineKeyboardMarkup(
            keyboard
        ),
        parse_mode="Markdown",
    )


# ============================================================
# REFERRALS
# ============================================================

async def referrals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    bal, count = get_stats(
        query.from_user.id
    )

    await query.message.edit_text(
        "👥 *My Referrals*\n\n"
        f"Successful referrals: *{count}*\n"
        f"Earned: *₦{count * REFERRAL_REWARD:,}*\n"
        f"Current balance: *₦{bal:,}*",
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


# ============================================================
# BALANCE
# ============================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    bal, count = get_stats(
        query.from_user.id
    )

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


# ============================================================
# REFERRAL LINK
# ============================================================

async def referral_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    me = await context.bot.get_me()

    link = (
        f"https://t.me/"
        f"{me.username}"
        f"?start={query.from_user.id}"
    )

    await query.message.edit_text(
        "🔗 *Your Referral Link*\n\n"
        "Share this link with your friends.\n\n"
        f"`{link}`\n\n"
        f"💰 You earn *₦{REFERRAL_REWARD:,}* "
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


# ============================================================
# WITHDRAWAL
# ============================================================

async def withdrawal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    bal, _ = get_stats(user_id)

    if bal < MIN_WITHDRAWAL:

        await query.message.edit_text(
            "❌ *Withdrawal Unavailable*\n\n"
            f"Your balance: *₦{bal:,}*\n"
            f"Minimum withdrawal: "
            f"*₦{MIN_WITHDRAWAL:,}*",
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

        return

    await query.message.edit_text(
        "💸 *Withdrawal Request*\n\n"
        f"Available balance: *₦{bal:,}*\n\n"
        "Please send your bank details in "
        "this format:\n\n"
        "`Bank Name`\n"
        "`Account Number`\n"
        "`Account Name`\n\n"
        "Example:\n"
        "`Access Bank`\n"
        "`0123456789`\n"
        "`John Doe`",
        parse_mode="Markdown",
    )

    context.user_data["waiting_for_bank_details"] = True
        "waiting_for_b            with conn.cursor() as cursor:
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
                    """ UPDATE users SET referral_rewarded = 1 WHERE user_id = %s """,
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.username)

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

        print(
            f"Membership check: user={user_id}, "
            f"status={member.status}"
        )

        telegram_joined = member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:
        print(f"Telegram membership check error: {e}")
        telegram_joined = False

    if telegram_joined:
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
            "❌ *Telegram Join Not Detected*\n\n"
            "Please join the Telegram channel first, "
            "then tap *Check Again*.\n\n"
            "If you have already joined, make sure "
            "the bot is an administrator of the channel.",
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
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start Freecash_bot"),
        BotCommand("admin", "Open admin panel"),
    ])
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))

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
