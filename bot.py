import os
import threading
import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, request, Response
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

TELEGRAM_CHANNEL = "@Vickyupdatemayor"
TELEGRAM_LINK = "https://t.me/Vickyupdatemayor"
WHATSAPP_CHANNEL = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"

REFERRAL_REWARD = 100
MINIMUM_WITHDRAWAL = 700

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
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        balance INTEGER DEFAULT 0,
                        referrals INTEGER DEFAULT 0,
                        referred_by BIGINT,
                        referral_rewarded INTEGER DEFAULT 0
                    )
                """)

                conn.commit()

        finally:
            conn.close()


def get_user(user_id, username=None):
    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM users WHERE user_id = %s",
                    (user_id,)
                )

                row = cursor.fetchone()

                if not row:
                    cursor.execute(
                        """
                        INSERT INTO users (user_id, username)
                        VALUES (%s, %s)
                        """,
                        (user_id, username)
                    )

                    conn.commit()

                    cursor.execute(
                        "SELECT * FROM users WHERE user_id = %s",
                        (user_id,)
                    )

                    row = cursor.fetchone()

                return row

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
                    "SELECT referred_by FROM users WHERE user_id = %s",
                    (user_id,)
                )

                user = cursor.fetchone()

                if not user:
                    return False

                if user[0] is not None:
                    return False

                cursor.execute(
                    "SELECT user_id FROM users WHERE user_id = %s",
                    (referrer_id,)
                )

                referrer = cursor.fetchone()

                if not referrer:
                    return False

                cursor.execute(
                    """
                    UPDATE users
                    SET referred_by = %s
                    WHERE user_id = %s
                    """,
                    (referrer_id, user_id)
                )

                conn.commit()
                return True

        finally:
            conn.close()


def reward_referrer(user_id):
    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT referred_by, referral_rewarded
                    FROM users
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )

                row = cursor.fetchone()

                if not row:
                    return False

                referrer_id, rewarded = row

                if referrer_id is None or rewarded:
                    return False

                cursor.execute(
                    """
                    UPDATE users
                    SET balance = balance + %s,
                        referrals = referrals + 1
                    WHERE user_id = %s
                    """,
                    (REFERRAL_REWARD, referrer_id)
                )

                cursor.execute(
                    """
                    UPDATE users
                    SET referral_rewarded = 1
                    WHERE user_id = %s
                    """,
                    (user_id,)
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
                    """
                    SELECT balance, referrals
                    FROM users
                    WHERE user_id = %s
                    """,
                    (user_id,)
                )

                row = cursor.fetchone()

                if row:
                    return row

                return 0, 0

        finally:
            conn.close()


# =========================
# FLASK SERVER
# =========================

app = Flask(__name__)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def check_admin_auth():
    if not ADMIN_PASSWORD:
        return False

    auth = request.authorization

    if not auth:
        return False

    return (
        auth.username == "admin"
        and auth.password == ADMIN_PASSWORD
    )


@app.route("/admin")
def admin_panel():

    if not check_admin_auth():
        return Response(
            "Admin login required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Vicky Admin Panel"'}
        )

    with db_lock:
        conn = get_connection()

        try:
            with conn.cursor() as cursor:

                cursor.execute(
                    "SELECT COUNT(*) FROM users"
                )
                total_users = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COALESCE(SUM(balance), 0) FROM users"
                )
                total_balance = cursor.fetchone()[0]

                cursor.execute(
                    "SELECT COALESCE(SUM(referrals), 0) FROM users"
                )
                total_referrals = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT user_id, username, balance, referrals
                    FROM users
                    ORDER BY user_id DESC
                    LIMIT 100
                """)

                users = cursor.fetchall()

        finally:
            conn.close()

    rows = ""

    for user_id, username, balance, referrals in users:

        username = username or "No username"

        rows += f"""
        <tr>
            <td>{user_id}</td>
            <td>{username}</td>
            <td>₦{balance:,}</td>
            <td>{referrals}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Vicky Admin Panel</title>

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 20px;
                background: #f5f5f5;
            }}

            h1 {{
                margin-bottom: 20px;
            }}

            .cards {{
                display: grid;
                grid-template-columns:
                    repeat(auto-fit, minmax(180px, 1fr));
                gap: 15px;
                margin-bottom: 25px;
            }}

            .card {{
                background: white;
                padding: 20px;
                border-radius: 12px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            }}

            .number {{
                font-size: 25px;
                font-weight: bold;
                margin-top: 8px;
            }}

            .table-container {{
                overflow-x: auto;
                background: white;
                border-radius: 12px;
                padding: 10px;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
                min-width: 600px;
            }}

            th, td {{
                padding: 12px;
                border-bottom: 1px solid #ddd;
                text-align: left;
            }}

            th {{
                background: #f0f0f0;
            }}
        </style>
    </head>

    <body>

        <h1>🔐 Vicky Admin Panel</h1>

        <div class="cards">

            <div class="card">
                👥 Total Users
                <div class="number">
                    {total_users}
                </div>
            </div>

            <div class="card">
                💰 Total Balance
                <div class="number">
                    ₦{total_balance:,}
                </div>
            </div>

            <div class="card">
                🤝 Total Referrals
                <div class="number">
                    {total_referrals}
                </div>
            </div>

        </div>

        <h2>Users</h2>

        <div class="table-container">

            <table>

                <thead>
                    <tr>
                        <th>Telegram ID</th>
                        <th>Username</th>
                        <th>Balance</th>
                        <th>Referrals</th>
                    </tr>
                </thead>

                <tbody>
                    {rows}
                </tbody>

            </table>

        </div>

    </body>
    </html>
    """

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

    balance, count = get_stats(query.from_user.id)

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

    bal, count = get_stats(query.from_user.id)

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

    bal, count = get_stats(query.from_user.id)

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

    if not DATABASE_URL:
        raise ValueError(
            "DATABASE_URL environment variable is missing."
        )

    init_database()

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
