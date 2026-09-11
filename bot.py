import logging
import os
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    PicklePersistence,
    filters,
)

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8361977048:AAE1hukuUYJsgtE7uwNRTL5oZpmsNdJU6Ns")

WHATSAPP_LINK = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"
TELEGRAM_GROUP_LINK = "https://t.me/Vickyupdatemayor"
TELEGRAM_GROUP_USERNAME = "@Vickyupdatemayor"

# Numeric ID of your Admin Channel (MUST start with -100)
ADMIN_CHANNEL_ID = -100448791708  # Replace with your real Admin Channel ID

# YOUR Personal Telegram User ID (Get yours from @userinfobot)
ADMIN_USER_ID = 6225743234  # Replace with your personal Telegram ID

MIN_WITHDRAWAL = 700
REFERRAL_BONUS = 75
# =======================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Conversation States
BANK_NAME, ACCOUNT_NUMBER, ACCOUNT_NAME, AMOUNT = range(4)
WA_PROOF = 4


# --- HEALTH CHECK SERVER ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is live!")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


# --- PERSISTENT DATA HELPERS ---
def get_user_data(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if "users" not in context.bot_data:
        context.bot_data["users"] = {}
    
    if user_id not in context.bot_data["users"]:
        context.bot_data["users"][user_id] = {
            "balance": 0,
            "referrals": 0,
            "bank_name": None,
            "acc_num": None,
            "acc_name": None,
            "bonus_credited": False,
            "referred_by": None,
            "wa_verified": False
        }
    return context.bot_data["users"][user_id]


def get_pending_withdrawals(context: ContextTypes.DEFAULT_TYPE):
    if "pending_withdrawals" not in context.bot_data:
        context.bot_data["pending_withdrawals"] = {}
    return context.bot_data["pending_withdrawals"]


async def is_user_subscribed_tg(bot, user_id):
    try:
        member = await bot.get_chat_member(chat_id=TELEGRAM_GROUP_USERNAME, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Error checking Telegram subscription: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_data = get_user_data(context, user_id)

    if context.args and context.args[0].isdigit():
        referrer_id = int(context.args[0])
        if referrer_id != user_id and user_data["referred_by"] is None:
            user_data["referred_by"] = referrer_id

    tg_joined = await is_user_subscribed_tg(context.bot, user_id)
    wa_verified = user_data.get("wa_verified", False)

    if not (tg_joined and wa_verified):
        keyboard = [
            [InlineKeyboardButton("1️⃣ Join WhatsApp Channel 🟢", url=WHATSAPP_LINK)],
            [InlineKeyboardButton("2️⃣ Join Telegram Group ✈️", url=TELEGRAM_GROUP_LINK)],
            [InlineKeyboardButton("Joined ✅", callback_data="check_joined")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⚠️ <b>Mandatory Verification Required!</b>\n\n"
            "To access the bot and start earning, you must join both channels below:\n\n"
            "1. Join our <b>WhatsApp Channel</b>\n"
            "2. Join our <b>Telegram Group</b>\n\n"
            "Click <b>Joined ✅</b> once completed.",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
        return

    await send_main_menu(update, context)


async def check_joined_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user_data = get_user_data(context, user_id)

    tg_joined = await is_user_subscribed_tg(context.bot, user_id)
    
    if not tg_joined:
        await query.message.reply_text("❌ You have not joined our Telegram group yet! Please join and try again.")
        return

    if not user_data.get("wa_verified", False):
        await query.message.reply_text(
            "📲 <b>WhatsApp Verification Required</b>\n\n"
            "Please send your <b>WhatsApp Name or Phone Number</b> (or send a screenshot proving you joined the channel) right here:",
            parse_mode="HTML"
        )
        return WA_PROOF

    await send_main_menu_direct(user_id, context)


async def receive_wa_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(context, user_id)

    user_data["wa_verified"] = True

    referrer_id = user_data.get("referred_by")
    if referrer_id and not user_data.get("bonus_credited"):
        ref_data = get_user_data(context, referrer_id)
        ref_data["balance"] += REFERRAL_BONUS
        ref_data["referrals"] += 1
        user_data["bonus_credited"] = True
        try:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=f"🎉 <b>New Referral!</b> You earned ₦{REFERRAL_BONUS}. Your new balance is ₦{ref_data['balance']}.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to notify referrer {referrer_id}: {e}")

    await update.message.reply_text("✅ <b>Verification Successful!</b> Welcome to the bot.", parse_mode="HTML")
    await send_main_menu(update, context)
    return ConversationHandler.END


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Select an option from the main menu below:", reply_markup=reply_markup)


async def send_main_menu_direct(chat_id, context):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await context.bot.send_message(chat_id=chat_id, text="Select an option from the main menu below:", reply_markup=reply_markup)


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(context, user_id)
    text = (
        f"💼 <b>Your Wallet</b>\n\n"
        f"💰 Balance: ₦{data['balance']}\n"
        f"👥 Total Referrals: {data['referrals']}\n\n"
        f"📌 <i>Minimum withdrawal limit is ₦{MIN_WITHDRAWAL}</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user_data(context, user_id)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    
    text = (
        f"👥 <b>Refer & Earn Program</b>\n\n"
        f"Share your link below with friends. Earn <b>₦{REFERRAL_BONUS}</b> instantly for every user who joins using your link!\n\n"
        f"🔗 Your referral link:\n<code>{ref_link}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def start_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(context, user_id)

    if data["balance"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Insufficient balance! Minimum withdrawal amount is <b>₦{MIN_WITHDRAWAL}</b>.\n"
            f"Your current balance: ₦{data['balance']}",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    if data["bank_name"] and data["acc_num"] and data["acc_name"]:
        context.user_data["bank_name"] = data["bank_name"]
        context.user_data["acc_num"] = data["acc_num"]
        context.user_data["acc_name"] = data["acc_name"]
        await update.message.reply_text(
            f"🏦 <b>Saved Payment Details Found:</b>\n"
            f"• Bank: {data['bank_name']}\n"
            f"• Account Number: {data['acc_num']}\n"
            f"• Account Name: {data['acc_name']}\n\n"
            f"Enter the amount you wish to withdraw (Minimum ₦{MIN_WITHDRAWAL}):",
            parse_mode="HTML"
        )
        return AMOUNT

    await update.message.reply_text("🏦 Enter your <b>Bank Name</b> (e.g., Access Bank, OPay, Palmpay):", parse_mode="HTML")
    return BANK_NAME


async def get_bank_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bank_name"] = update.message.text.strip()
    await update.message.reply_text("💳 Enter your <b>Account Number</b>:", parse_mode="HTML")
    return ACCOUNT_NUMBER


async def get_account_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acc_num = update.message.text.strip()
    if not acc_num.isdigit():
        await update.message.reply_text("❌ Invalid account number. Enter numbers only:")
        return ACCOUNT_NUMBER

    context.user_data["acc_num"] = acc_num
    await update.message.reply_text("👤 Enter your <b>Account Name</b> (as registered on the bank account):", parse_mode="HTML")
    return ACCOUNT_NAME


async def get_account_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["acc_name"] = update.message.text.strip()
    await update.message.reply_text(f"💵 Enter the amount to withdraw (Minimum ₦{MIN_WITHDRAWAL}):", parse_mode="HTML")
    return AMOUNT


async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    data = get_user_data(context, user_id)
    text = update.message.text.strip()

    if not text.isdigit():
        await update.message.reply_text("❌ Please enter a valid number for the amount:")
        return AMOUNT

    amount = int(text)

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(f"❌ Minimum withdrawal is ₦{MIN_WITHDRAWAL}. Try again:")
        return AMOUNT

    if amount > data["balance"]:
        await update.message.reply_text(f"❌ You cannot withdraw more than your current balance (₦{data['balance']}). Try again:")
        return AMOUNT

    data["bank_name"] = context.user_data["bank_name"]
    data["acc_num"] = context.user_data["acc_num"]
    data["acc_name"] = context.user_data["acc_name"]
    data["balance"] -= amount

    # Generate Unique Request ID
    req_id = str(uuid.uuid4())[:8]
    
    # Store request in pending queue
    pending = get_pending_withdrawals(context)
    pending[req_id] = {
        "user_id": user_id,
        "full_name": user.full_name,
        "amount": amount,
        "bank_name": data["bank_name"],
        "acc_num": data["acc_num"],
        "acc_name": data["acc_name"]
    }

    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"app_{req_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rej_{req_id}")
        ]
    ]
    admin_markup = InlineKeyboardMarkup(admin_keyboard)

    full_name_clean = user.full_name.replace("<", "&lt;").replace(">", "&gt;")
    bank_clean = data['bank_name'].replace("<", "&lt;").replace(">", "&gt;")
    acc_name_clean = data['acc_name'].replace("<", "&lt;").replace(">", "&gt;")

    admin_msg = (
        f"🚨 <b>New Withdrawal Request</b> [ID: <code>{req_id}</code>]\n\n"
        f"👤 User: {full_name_clean} (<code>{user_id}</code>)\n"
        f"💵 Amount: ₦{amount}\n"
        f"🏦 Bank: {bank_clean}\n"
        f"💳 Acc No: <code>{data['acc_num']}</code>\n"
        f"👤 Acc Name: {acc_name_clean}"
    )

    # Attempt sending to Admin Channel
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHANNEL_ID,
            text=admin_msg,
            reply_markup=admin_markup,
            parse_mode="HTML"
        )
        logging.info(f"Withdrawal request {req_id} sent to admin channel for user {user_id}")
    except Exception as e:
        logging.error(f"CRITICAL: Could not deliver message to ADMIN_CHANNEL_ID ({ADMIN_CHANNEL_ID}): {e}")
        logging.info(f"Request {req_id} is saved in pending storage and can be reviewed via /pending command.")

    await update.message.reply_text("✅ Your withdrawal request has been submitted to the admin for review!")
    return ConversationHandler.END


async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Process cancelled.")
    return ConversationHandler.END


async def admin_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    action = data[0]
    req_id = data[1]

    pending = get_pending_withdrawals(context)

    # Handle legacy callback format if user clicks old buttons
    if len(data) == 3:
        user_id = int(data[1])
        amount = int(data[2])
        req_id = None
    else:
        req_info = pending.get(req_id)
        if not req_info and req_id is not None:
            await query.edit_message_text("⚠️ This request was already processed.")
            return
        user_id = req_info["user_id"]
        amount = req_info["amount"]

    if action == "app":
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>Withdrawal Approved!</b>\n\nYour withdrawal request for ₦{amount} has been processed.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to notify user {user_id} of approval: {e}")
            
        await query.edit_message_text(text=query.message.text_html + "\n\n🟢 <b>STATUS: APPROVED</b>", parse_mode="HTML")

    elif action == "rej":
        user_data = get_user_data(context, user_id)
        user_data["balance"] += amount
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ <b>Withdrawal Rejected.</b>\n\nYour withdrawal request for ₦{amount} was declined by the admin. The funds have been refunded to your wallet.",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to notify user {user_id} of rejection: {e}")
            
        await query.edit_message_text(text=query.message.text_html + "\n\n🔴 <b>STATUS: REJECTED</b>", parse_mode="HTML")

    # Clear from pending queue
    if req_id and req_id in pending:
        del pending[req_id]


async def list_pending_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ADMIN_USER_ID != 123456789 and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to view pending withdrawals.")
        return

    pending = get_pending_withdrawals(context)

    if not pending:
        await update.message.reply_text("🎉 **No pending withdrawal requests found!**", parse_mode="Markdown")
        return

    await update.message.reply_text(f"📋 **Found {len(pending)} pending withdrawal request(s):**\n")

    for req_id, req in list(pending.items()):
        admin_keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"app_{req_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_{req_id}")
            ]
        ]
        admin_markup = InlineKeyboardMarkup(admin_keyboard)

        full_name_clean = req['full_name'].replace("<", "&lt;").replace(">", "&gt;")
        bank_clean = req['bank_name'].replace("<", "&lt;").replace(">", "&gt;")
        acc_name_clean = req['acc_name'].replace("<", "&lt;").replace(">", "&gt;")

        msg = (
            f"🚨 <b>Pending Request</b> [ID: <code>{req_id}</code>]\n\n"
            f"👤 User: {full_name_clean} (<code>{req['user_id']}</code>)\n"
            f"💵 Amount: ₦{req['amount']}\n"
            f"🏦 Bank: {bank_clean}\n"
            f"💳 Acc No: <code>{req['acc_num']}</code>\n"
            f"👤 Acc Name: {acc_name_clean}"
        )
        await update.message.reply_text(msg, reply_markup=admin_markup, parse_mode="HTML")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ADMIN_USER_ID != 123456789 and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use the admin panel.")
        return

    users = context.bot_data.get("users", {})
    pending = context.bot_data.get("pending_withdrawals", {})
    total_users = len(users)
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_referrals = sum(u.get("referrals", 0) for u in users.values())

    stats_msg = (
        f"⚙️ <b>Admin Dashboard</b>\n\n"
        f"👥 <b>Total Registered Users:</b> {total_users}\n"
        f"💰 <b>Total Active User Balances:</b> ₦{total_balance}\n"
        f"🔗 <b>Total Successful Referrals:</b> {total_referrals}\n"
        f"⏳ <b>Pending Withdrawals:</b> {len(pending)}\n\n"
        f"📌 <i>Type /pending to review all pending requests.</i>"
    )
    await update.message.reply_text(stats_msg, parse_mode="HTML")


def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()

    persistence = PicklePersistence(filepath="bot_data.pkl")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    withdraw_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("(?i).*(withdraw).*"), start_withdrawal),
            CommandHandler("withdraw", start_withdrawal)
        ],
        states={
            BANK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bank_name)],
            ACCOUNT_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_account_number)],
            ACCOUNT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_account_name)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel_withdrawal)],
    )

    wa_proof_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(check_joined_callback, pattern="^check_joined$")],
        states={
            WA_PROOF: [MessageHandler(filters.TEXT | filters.PHOTO, receive_wa_proof)]
        },
        fallbacks=[CommandHandler("cancel", cancel_withdrawal)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("pending", list_pending_withdrawals))
    app.add_handler(wa_proof_handler)
    app.add_handler(CallbackQueryHandler(admin_decision_callback, pattern="^(app|rej)_"))

    app.add_handler(MessageHandler(filters.Regex("(?i).*(balance|wallet).*"), show_balance))
    app.add_handler(MessageHandler(filters.Regex("(?i).*(refer|earn|invite).*"), show_referral))
    app.add_handler(withdraw_handler)

    logging.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
