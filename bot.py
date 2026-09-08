import os
import logging
import threading
from flask import Flask
import psycopg2
from psycopg2 import pool
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)

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
WITHDRAW_AMOUNT, WITHDRAW_METHOD, WITHDRAW_DETAILS = range(3)
MIN_WITHDRAWAL = 700

def init_database():
    global db_pool
    db_pool = pool.SimpleConnectionPool(1, 5, dsn=DATABASE_URL, sslmode="require")
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(""" CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, referrals INTEGER DEFAULT 0, referred_by BIGINT, referral_rewarded INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ) """)
            cursor.execute(""" CREATE TABLE IF NOT EXISTS withdrawals ( id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount INTEGER NOT NULL, method TEXT NOT NULL, details TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ) """)
            cursor.execute(""" ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP """)
            conn.commit()
    finally:
        db_pool.putconn(conn)

def db_execute(query, params=(), fetchone=False, fetchall=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cursor:
            cursor.execute(query, params)
            result = cursor.fetchone() if fetchone else cursor.fetchall() if fetchall else None
            conn.commit()
            return result
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def register_user(user_id, username, referred_by=None):
    existing = db_execute("SELECT user_id FROM users WHERE user_id=%s", (user_id,), fetchone=True)
    if existing:
        db_execute("UPDATE users SET username=%s WHERE user_id=%s", (username, user_id))
        return False
    valid_referrer = None
    if referred_by and referred_by != user_id:
        if db_execute("SELECT user_id FROM users WHERE user_id=%s", (referred_by,), fetchone=True):
            valid_referrer = referred_by
    db_execute(""" INSERT INTO users (user_id, username, balance, referrals, referred_by, referral_rewarded) VALUES (%s,%s,0,0,%s,0) """, (user_id, username, valid_referrer))
    if valid_referrer:
        db_execute(""" UPDATE users SET referrals=referrals+1, balance=balance+1 WHERE user_id=%s """, (valid_referrer,))
    return True

def get_user(user_id):
    return db_execute(""" SELECT user_id, username, balance, referrals, referred_by FROM users WHERE user_id=%s """, (user_id,), fetchone=True)

WELCOME_TEXT = (
    "👋 <b>Welcome to Vicky Join Bot!</b>\n\n"
    "You're successfully connected.\n\n"
    "Use the buttons below to check your balance, referrals, "
    "invite friends, or request a withdrawal."
)

HELP_TEXT = (
    "🤖 <b>Bot Commands</b>\n\n"
    "/start - Open the main menu\n"
    "/balance - Check your balance\n"
    "/referrals - Check your referrals\n"
    "/invite - Get your referral link\n"
    "/withdraw - Request a withdrawal\n"
    "/help - Show this help message"
)

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
            InlineKeyboardButton("👥 Referrals", callback_data="referrals"),
        ],
        [InlineKeyboardButton("🔗 Invite Friends", callback_data="invite")],
        [InlineKeyboardButton("💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if context.args:
        value = context.args[0].strip()
        try:
            referred_by = int(value[4:] if value.startswith("ref_") else value)
        except ValueError:
            pass
    register_user(user.id, user.username or "", referred_by)
    await update.message.reply_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=main_keyboard())

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        register_user(user.id, user.username or "")
        row = get_user(user.id)
    await update.message.reply_text(
        f"💰 <b>Your Balance</b>\n\nBalance: <b>{row[2]}</b>\nReferrals: <b>{row[3]}</b>",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = get_user(update.effective_user.id)
    if not row:
        register_user(update.effective_user.id, update.effective_user.username or "")
        row = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"👥 <b>Your Referrals</b>\n\nYou have referred <b>{row[3]}</b> user(s).\n"
        f"Your balance is <b>{row[2]}</b>.",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot = await context.bot.get_me()
    link = f"https://t.me/{bot.username}?start=ref_{user.id}"
    await update.message.reply_text(
        f"🔗 <b>Your Referral Link</b>\n\n<code>{link}</code>\n\nSend this link to your friends.",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=main_keyboard())

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    row = get_user(user.id)
    if not row:
        register_user(user.id, user.username or "")
        row = get_user(user.id)
    if row[2] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"💸 <b>Withdrawal</b>\n\nYour balance: <b>{row[2]}</b>\n"
            f"Minimum withdrawal: <b>{MIN_WITHDRAWAL}</b>\n\n"
            "Keep earning until you reach the minimum.",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    await update.message.reply_text(
        f"💸 <b>Withdrawal Request</b>\n\n"
        f"Available balance: <b>{row[2]}</b>\n"
        f"Minimum withdrawal: <b>{MIN_WITHDRAWAL}</b>\n\n"
        "Enter the amount you want to withdraw:",
        parse_mode="HTML"
    )
    return WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Enter a whole number only.")
        return WITHDRAW_AMOUNT
    row = get_user(update.effective_user.id)
    if not row or amount < MIN_WITHDRAWAL:
        await update.message.reply_text(f"❌ Minimum withdrawal is {MIN_WITHDRAWAL}. Try again.")
        return WITHDRAW_AMOUNT
    if amount > row[2]:
        await update.message.reply_text(f"❌ Insufficient balance. Your balance is {row[2]}. Try again.")
        return WITHDRAW_AMOUNT
    context.user_data["withdraw_amount"] = amount
    await update.message.reply_text(
        "💳 <b>Choose your withdrawal method</b>\n\n"
        "Type the method you want to use (for example: Bank, Airtm, USDT, etc.):",
        parse_mode="HTML"
    )
    return WITHDRAW_METHOD

async def withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = update.message.text.strip()
    if len(method) < 2:
        await update.message.reply_text("❌ Please enter a valid withdrawal method.")
        return WITHDRAW_METHOD
    context.user_data["withdraw_method"] = method
    await update.message.reply_text(
        "📩 Now send your payment details for that method "
        "(for example, account number/name or wallet address)."
    )
    return WITHDRAW_DETAILS

async def withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    details = update.message.text.strip()
    amount = context.user_data.get("withdraw_amount")
    method = context.user_data.get("withdraw_method")
    row = get_user(user.id)
    if not amount or not method or not details or not row:
        await update.message.reply_text("❌ Your withdrawal session expired. Please press Withdraw again.", reply_markup=main_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    # Reserve the funds immediately so the same balance cannot be withdrawn twice.
    reserved = db_execute(""" UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s RETURNING balance """, (amount, user.id, amount), fetchone=True)
    if not reserved:
        await update.message.reply_text("❌ Insufficient balance. Please start the withdrawal again.", reply_markup=main_keyboard())
        context.user_data.clear()
        return ConversationHandler.END

    withdrawal = db_execute(""" INSERT INTO withdrawals (user_id, amount, method, details, status) VALUES (%s,%s,%s,%s,'pending') RETURNING id """, (user.id, amount, method, details), fetchone=True)
    withdrawal_id = withdrawal[0]

    if ADMIN_ID:
        try:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"wd_approve_{withdrawal_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"wd_reject_{withdrawal_id}"),
            ]])
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=(
                    "💸 <b>New Withdrawal Request</b>\n\n"
                    f"Request ID: <code>{withdrawal_id}</code>\n"
                    f"User: @{user.username or 'no_username'}\n"
                    f"User ID: <code>{user.id}</code>\n"
                    f"Amount: <b>{amount}</b>\n"
                    f"Method: <b>{method}</b>\n"
                    f"Details: <code>{details}</code>\n\n"
                    "Status: <b>PENDING</b>"
                ),
                parse_mode="HTML", reply_markup=keyboard
            )
        except Exception:
            logger.exception("Could not notify admin")

    context.user_data.clear()
    await update.message.reply_text(
        "✅ <b>Withdrawal request submitted!</b>\n\n"
        f"Amount: <b>{amount}</b>\nMethod: <b>{method}</b>\n"
        "Your funds have been reserved and your request is pending admin review.",
        parse_mode="HTML", reply_markup=main_keyboard()
    )
    return ConversationHandler.END

async def admin_withdrawal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not ADMIN_ID or str(query.from_user.id) != str(ADMIN_ID):
        await query.answer("Not authorized.", show_alert=True)
        return

    parts = query.data.split("_")
    if len(parts) != 3 or parts[0] != "wd" or parts[1] not in ("approve", "reject"):
        return
    action, request_id = parts[1], parts[2]
    try:
        request_id = int(request_id)
    except ValueError:
        await query.edit_message_text("❌ Invalid withdrawal request.")
        return

    request = db_execute(""" SELECT id, user_id, amount, method, details, status FROM withdrawals WHERE id=%s """, (request_id,), fetchone=True)
    if not request:
        await query.edit_message_text("❌ Withdrawal request not found.")
        return

    req_id, user_id, amount, method, details, status = request
    if status != "pending":
        await query.answer(f"Already {status}.", show_alert=True)
        return

    if action == "approve":
        updated = db_execute(""" UPDATE withdrawals SET status='approved', processed_at=CURRENT_TIMESTAMP WHERE id=%s AND status='pending' RETURNING id """, (req_id,), fetchone=True)
        if not updated:
            await query.answer("Already processed.", show_alert=True)
            return
        new_text = (
            f"✅ <b>Withdrawal APPROVED</b>\n\nRequest ID: <code>{req_id}</code>\n"
            f"User ID: <code>{user_id}</code>\nAmount: <b>{amount}</b>\n"
            f"Method: <b>{method}</b>\nDetails: <code>{details}</code>"
        )
        user_text = f"✅ <b>Withdrawal Approved</b>\n\nAmount: <b>{amount}</b>\nMethod: <b>{method}</b>\nYour withdrawal has been approved."
    else:
        updated = db_execute(""" UPDATE withdrawals SET status='rejected', processed_at=CURRENT_TIMESTAMP WHERE id=%s AND status='pending' RETURNING id """, (req_id,), fetchone=True)
        if not updated:
            await query.answer("Already processed.", show_alert=True)
            return
        # Return the reserved funds when an admin rejects the request.
        db_execute("UPDATE users SET balance = balance + %s WHERE user_id=%s", (amount, user_id))
        new_text = (
            f"❌ <b>Withdrawal REJECTED</b>\n\nRequest ID: <code>{req_id}</code>\n"
            f"User ID: <code>{user_id}</code>\nAmount: <b>{amount}</b>\n"
            f"Method: <b>{method}</b>\nDetails: <code>{details}</code>\n\n"
            "The amount has been returned to the user's balance."
        )
        user_text = f"❌ <b>Withdrawal Rejected</b>\n\nAmount: <b>{amount}</b>\nMethod: <b>{method}</b>\nYour withdrawal was rejected and the amount has been returned to your balance."

    await query.edit_message_text(new_text, parse_mode="HTML")
    try:
        await context.bot.send_message(chat_id=user_id, text=user_text, parse_mode="HTML", reply_markup=main_keyboard())
    except Exception:
        logger.exception("Could not notify user about withdrawal decision")

async def withdraw_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Withdrawal cancelled.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    row = get_user(user_id)
    if not row:
        register_user(user_id, query.from_user.username or "")
        row = get_user(user_id)

    if query.data == "balance":
        text = f"💰 <b>Your Balance</b>\n\nBalance: <b>{row[2]}</b>\nReferrals: <b>{row[3]}</b>"
    elif query.data == "referrals":
        text = f"👥 <b>Your Referrals</b>\n\nYou have referred <b>{row[3]}</b> user(s).\nYour balance is <b>{row[2]}</b>."
    elif query.data == "invite":
        bot = await context.bot.get_me()
        link = f"https://t.me/{bot.username}?start=ref_{user_id}"
        text = f"🔗 <b>Your Referral Link</b>\n\n<code>{link}</code>\n\nSend this link to your friends."
    elif query.data == "help":
        text = HELP_TEXT
    else:
        return
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=main_keyboard())

flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return "Vicky Join Bot is running ✅", 200

@flask_app.get("/health")
def health_check():
    return "OK", 200

def run_web_server():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

def main():
    init_database()
    threading.Thread(target=run_web_server, daemon=True).start()
    application = Application.builder().token(BOT_TOKEN).build()

    withdrawal = ConversationHandler(
        entry_points=[
            CommandHandler("withdraw", withdraw_start),
            CallbackQueryHandler(withdraw_start, pattern="^withdraw$")
        ],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WITHDRAW_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_method)],
            WITHDRAW_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_details)],
        },
        fallbacks=[CommandHandler("cancel", withdraw_cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("referrals", referrals))
    application.add_handler(CommandHandler("invite", invite))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(withdrawal)
    application.add_handler(CallbackQueryHandler(admin_withdrawal_callback, pattern=r"^wd_(approve|reject)_\d+$"))
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Bot is now running.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
