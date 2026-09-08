import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
CHANNEL_USERNAME = "@YourChannelUsername"  # e.g., @mychannel (must include @)
CHANNEL_ID = -1001234567890              # Numeric ID of your channel (Bot must be Admin)
ADMIN_CHANNEL_ID = -1009876543210        # Numeric ID of your private Admin channel/group

MIN_WITHDRAWAL = 600
REFERRAL_BONUS = 100
# =======================================================

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# In-memory storage (Replace with SQLite/PostgreSQL database for production)
users_db = {} 
# Format: { user_id: {"balance": 0, "referrals": 0, "bank_name": "", "acc_num": "", "acc_name": ""} }

# States for withdrawal conversation
BANK_NAME, ACCOUNT_NUMBER, ACCOUNT_NAME, AMOUNT = range(4)


def get_user_data(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance": 0,
            "referrals": 0,
            "bank_name": None,
            "acc_num": None,
            "acc_name": None,
        }
    return users_db[user_id]


async def is_user_subscribed(bot, user_id):
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logging.error(f"Error checking subscription: {e}")
        return False


# --- START COMMAND & FORCE SUB ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    get_user_data(user_id)

    # Check for referral link (?start=REFERRER_ID)
    if context.args and context.args[0].isdigit():
        referrer_id = int(context.args[0])
        if referrer_id != user_id and "referred_by" not in users_db[user_id]:
            users_db[user_id]["referred_by"] = referrer_id

    # Force Sub Check
    if not await is_user_subscribed(context.bot, user_id):
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}")],
            [InlineKeyboardButton("Joined ✅", callback_data="check_joined")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"⚠️ You must join our channel {CHANNEL_USERNAME} to access the bot and start earning!",
            reply_markup=reply_markup
        )
        return

    await send_main_menu(update, context)


async def check_joined_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await is_user_subscribed(context.bot, user_id):
        # Credit referral bonus if user was referred and hasn't been credited yet
        referrer_id = users_db[user_id].get("referred_by")
        if referrer_id and not users_db[user_id].get("bonus_credited"):
            users_db[referrer_id]["balance"] += REFERRAL_BONUS
            users_db[referrer_id]["referrals"] += 1
            users_db[user_id]["bonus_credited"] = True
            
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 **New Referral!** You earned ₦{REFERRAL_BONUS}. Your new balance is ₦{users_db[referrer_id]['balance']}."
                )
            except Exception:
                pass

        await query.message.delete()
        await context.bot.send_message(chat_id=user_id, text="✅ Membership verified! Welcome.")
        await send_main_menu_direct(user_id, context)
    else:
        await query.message.reply_text("❌ You have not joined the channel yet. Please join and try again.")


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(" Welcome to the main menu! Select an option below:", reply_markup=reply_markup)


async def send_main_menu_direct(chat_id, context):
    keyboard = [
        ["💰 Balance / Wallet", "👥 Refer & Earn"],
        ["💸 Withdraw"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await context.bot.send_message(chat_id=chat_id, text=" Welcome to the main menu! Select an option below:", reply_markup=reply_markup)


# --- MAIN MENU HANDLERS ---
async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)
    text = (
        f"💼 **Your Wallet**\n\n"
        f"💰 Balance: ₦{data['balance']}\n"
        f"👥 Total Referrals: {data['referrals']}\n\n"
        f"📌 *Minimum withdrawal limit is ₦{MIN_WITHDRAWAL}*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    
    text = (
        f"👥 **Refer & Earn Program**\n\n"
        f"Share your link below with friends. Earn **₦{REFERRAL_BONUS}** instantly for every user who joins using your link!\n\n"
        f"🔗 Your referral link:\n`{ref_link}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# --- WITHDRAWAL CONVERSATION FLOW ---
async def start_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = get_user_data(user_id)

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
            f"🏦 Saved Payment Details Found:\n"
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
    data = get_user_data(user_id)
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

    # Save payment details for future use
    data["bank_name"] = context.user_data["bank_name"]
    data["acc_num"] = context.user_data["acc_num"]
    data["acc_name"] = context.user_data["acc_name"]

    # Deduct balance temporarily pending admin decision
    data["balance"] -= amount

    # Notify Admin Channel
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

    await context.bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_msg, reply_markup=admin_markup, parse_mode="Markdown")
    await update.message.reply_text("✅ Your withdrawal request has been submitted to the admin for review!")

    return ConversationHandler.END


async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Withdrawal process cancelled.")
    return ConversationHandler.END


# --- ADMIN CALLBACK ACTIONS ---
async def admin_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
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
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ **Withdrawal Rejected.**\n\nYour withdrawal request for ₦{amount} was declined by the admin.",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await query.edit_message_text(text=query.message.text + "\n\n🔴 **STATUS: REJECTED**", parse_mode="Markdown")


# --- MAIN BOT RUNNER ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation Handler for Withdrawals
    withdraw_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💸 Withdraw$"), start_withdrawal),
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

    # Base Command & Callback Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(check_joined_callback, pattern="^check_joined$"))
    app.add_handler(CallbackQueryHandler(admin_decision_callback, pattern="^(app|rej)_"))

    # Menu Message Handlers
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance / Wallet$"), show_balance))
    app.add_handler(MessageHandler(filters.Regex("^👥 Refer & Earn$"), show_referral))
    app.add_handler(withdraw_handler)

    logging.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
        
