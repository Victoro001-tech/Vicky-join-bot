import logging
import os
import threading
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
ADMIN_CHANNEL_ID = -1001234567890  # Replace with your real Admin Channel ID

# YOUR Personal Telegram User ID (Get yours from @userinfobot)
ADMIN_USER_ID = 123456789  # Replace with your personal Telegram ID

MIN_WITHDRAWAL = 600
REFERRAL_BONUS = 100
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


# --- PERSISTENT DATA HELPER ---
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
            "⚠️ **Mandatory Verification Required!**\n\n"
            "To access the bot and start earning, you must join both channels below:\n\n"
            "1. Join our **WhatsApp Channel**\n"
            "2. Join our **Telegram Group**\n\n"
            "Click **Joined ✅** once completed.",
            reply_markup=reply_markup,
            parse_mode="Markdown"
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
            "📲 **WhatsApp Verification Required**\n\n"
            "Please send your **WhatsApp Name or Phone Number** (or send a screenshot proving you joined the channel) right here to submit for verification:"
        )
        return WA_PROOF

    await send_main_menu_direct(user_id, context)


async def receive_wa_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Verify WA", callback_data=f"vwa_{user_id}"),
            InlineKeyboardButton("❌ Reject WA", callback_data=f"rwa_{user_id}")
        ]
    ]
    admin_markup = InlineKeyboardMarkup(admin_keyboard)

    admin_msg = (
        f"📲 **New WhatsApp Join Verification Request**\n\n"
        f"👤 User: {user.full_name} (`{user_id}`)\n"
    )

    try:
        if update.message.photo:
            photo_id = update.message.photo[-1].file_id
            await context.bot.send_photo(
                chat_id=ADMIN_CHANNEL_ID,
                photo=photo_id,
                caption=admin_msg + "🖼 Proof: Screenshot attached below.",
                reply_markup=admin_markup,
                parse_mode="Markdown"
            )
        else:
            proof_text = update.message.text
            await context.bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=admin_msg + f"💬 Proof Text: `{proof_text}`",
                reply_markup=admin_markup,
                parse_mode="Markdown"
            )
    except Exception as e:
        logging.error(f"Failed to send proof to admin channel: {e}")

    await update.message.reply_text("✅ Proof submitted! Your WhatsApp verification request is under admin review.")
    return ConversationHandler.END


async def admin_wa_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Restrict button action to the primary admin only
    if ADMIN_USER_ID != 123456789 and query.from_user.id != ADMIN_USER_ID:
        await query.answer("❌ Only the main admin is authorized to approve or reject requests!", show_alert=True)
        return

    await query.answer()

    data = query.data.split("_")
    action = data[0]
    user_id = int(data[1])
    user_data = get_user_data(context, user_id)

    if action == "vwa":
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
                    text=f"🎉 **New Referral!** You earned ₦{REFERRAL_BONUS}. Your new balance is ₦{ref_data['balance']}."
                )
            except Exception:
                pass

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="✅ **WhatsApp Verification Approved!**\n\nWelcome to the bot! Tap /start to open the main menu."
            )
        except Exception:
            pass

        if query.message.photo:
            await query.edit_message_caption(caption=query.message.caption + "\n\n🟢 **WA VERIFIED**")
        else:
            await query.edit_message_text(text=query.message.text + "\n\n🟢 **WA VERIFIED**")

    elif action == "rwa":
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ **WhatsApp Verification Rejected.**\nPlease ensure you join the WhatsApp channel and submit valid proof."
            )
        except Exception:
            pass

        if query.message.photo:
            await query.edit_message_caption(caption=query.message.caption + "\n\n🔴 **WA REJECTED**")
        else:
            await query.edit_message_text(text=query.message.text + "\n\n🔴 **WA REJECTED**")


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Welcome to the main menu! Select an option below:", reply_markup=reply_markup)


async def send_main_menu_direct(chat_id, context):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await context.bot.send_message(chat_id=chat_id, text="Welcome to the main menu! Select an option below:", reply_markup=reply_markup)


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(context, user_id)
    text = (
        f"💼 **Your Wallet**\n\n"
        f"💰 Balance: ₦{data['balance']}\n"
        f"👥 Total Referrals: {data['referrals']}\n\n"
        f"📌 *Minimum withdrawal limit is ₦{MIN_WITHDRAWAL}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user_data(context, user_id)
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    
    text = (
        f"👥 **Refer & Earn Program**\n\n"
        f"Share your link below with friends. Earn **₦{REFERRAL_BONUS}** instantly for every user who joins using your link!\n\n"
        f"🔗 Your referral link:\n`{ref_link}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def start_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(context, user_id)

    if data["balance"] < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Insufficient balance! Minimum withdrawal amount is **₦{MIN_WITHDRAWAL}**.\n"
            f"Your current balance: ₦{data['balance']}",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if data["bank_name"] and data["acc_num"] and data["acc_name"]:
        context.user_data["bank_name"] = data["bank_name"]
        context.user_data["acc_num"] = data["acc_num"]
        context.user_data["acc_name"] = data["acc_name"]
        await update.message.reply_text(
            f"🏦 **Saved Payment Details Found:**\n"
            f"• Bank: {data['bank_name']}\n"
            f"• Account Number: {data['acc_num']}\n"
            f"• Account Name: {data['acc_name']}\n\n"
            f"Enter the amount you wish to withdraw (Minimum ₦{MIN_WITHDRAWAL}):"
        )
        return AMOUNT

    await update.message.reply_text("🏦 Enter your **Bank Name** (e.g., Access Bank, OPay, Palmpay):")
    return BANK_NAME


async def get_bank_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bank_name"] = update.message.text.strip()
    await update.message.reply_text("💳 Enter your **Account Number**:")
    return ACCOUNT_NUMBER


async def get_account_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acc_num = update.message.text.strip()
    if not acc_num.isdigit():
        await update.message.reply_text("❌ Invalid account number. Enter numbers only:")
        return ACCOUNT_NUMBER

    context.user_data["acc_num"] = acc_num
    await update.message.reply_text("👤 Enter your **Account Name** (as registered on the bank account):")
    return ACCOUNT_NAME


async def get_account_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["acc_name"] = update.message.text.strip()
    await update.message.reply_text(f"💵 Enter the amount to withdraw (Minimum ₦{MIN_WITHDRAWAL}):")
    return AMOUNT


async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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

    admin_keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"app_{user_id}_{amount}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user_id}_{amount}")
        ]
    ]
    admin_markup = InlineKeyboardMarkup(admin_keyboard)

    admin_msg = (
        f"🚨 **New Withdrawal Request**\n\n"
        f"👤 User: {update.effective_user.full_name} (`{user_id}`)\n"
        f"💵 Amount: ₦{amount}\n"
        f"🏦 Bank: {data['bank_name']}\n"
        f"💳 Acc No: `{data['acc_num']}`\n"
        f"👤 Acc Name: {data['acc_name']}"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_msg, reply_markup=admin_markup, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send withdrawal to admin channel: {e}")

    await update.message.reply_text("✅ Your withdrawal request has been submitted to the admin for review!")

    return ConversationHandler.END


async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Process cancelled.")
    return ConversationHandler.END


async def admin_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Restrict button action to the primary admin only
    if ADMIN_USER_ID != 123456789 and query.from_user.id != ADMIN_USER_ID:
        await query.answer("❌ Only the main admin is authorized to approve or reject requests!", show_alert=True)
        return

    await query.answer()

    data = query.data.split("_")
    action = data[0]
    user_id = int(data[1])
    amount = int(data[2])

    if action == "app":
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ **Withdrawal Approved!**\n\nYour withdrawal request for ₦{amount} has been processed.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await query.edit_message_text(text=query.message.text + "\n\n🟢 **STATUS: APPROVED**", parse_mode="Markdown")

    elif action == "rej":
        user_data = get_user_data(context, user_id)
        user_data["balance"] += amount
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ **Withdrawal Rejected.**\n\nYour withdrawal request for ₦{amount} was declined by the admin. The funds have been refunded to your wallet.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await query.edit_message_text(text=query.message.text + "\n\n🔴 **STATUS: REJECTED**", parse_mode="Markdown")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if ADMIN_USER_ID != 123456789 and user_id != ADMIN_USER_ID:
        await update.message.reply_text("❌ You are not authorized to use the admin panel.")
        return

    users = context.bot_data.get("users", {})
    total_users = len(users)
    total_balance = sum(u.get("balance", 0) for u in users.values())
    total_referrals = sum(u.get("referrals", 0) for u in users.values())

    stats_msg = (
        f"⚙️ **Admin Dashboard**\n\n"
        f"👥 **Total Registered Users:** {total_users}\n"
        f"💰 **Total Active User Balances:** ₦{total_balance}\n"
        f"🔗 **Total Successful Referrals:** {total_referrals}\n\n"
        f"📌 *Withdrawal & WA approvals are managed in your Admin Channel.*"
    )
    await update.message.reply_text(stats_msg, parse_mode="Markdown")


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
    app.add_handler(wa_proof_handler)
    app.add_handler(CallbackQueryHandler(admin_wa_decision_callback, pattern="^(vwa|rwa)_"))
    app.add_handler(CallbackQueryHandler(admin_decision_callback, pattern="^(app|rej)_"))

    app.add_handler(MessageHandler(filters.Regex("(?i).*(balance|wallet).*"), show_balance))
    app.add_handler(MessageHandler(filters.Regex("(?i).*(refer|earn|invite).*"), show_referral))
    app.add_handler(withdraw_handler)

    logging.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
