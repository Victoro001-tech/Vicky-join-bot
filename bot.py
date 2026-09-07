import os
import threading

from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

TELEGRAM_CHANNEL = "@Vickyupdatemayor"

WHATSAPP_CHANNEL = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"

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
    await show_join_page(update, context)


async def show_join_page(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "📢 Join Telegram Channel",
                url="https://t.me/Vickyupdatemayor"
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
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    try:
        member = await context.bot.get_chat_member(
            chat_id=TELEGRAM_CHANNEL,
            user_id=user_id
        )

        status = member.status

        telegram_joined = status in [
            "member",
            "administrator",
            "creator"
        ]

    except Exception:
        telegram_joined = False

    if telegram_joined:

        keyboard = [
            [
                InlineKeyboardButton(
                    "🚀 Continue",
                    callback_data="continue"
                )
            ]
        ]

        await query.message.edit_text(
            "✅ *Telegram Channel:* Joined\n"
            "🟢 *WhatsApp Channel:* Completed\n\n"
            "🎉 Your requirements are complete!\n\n"
            "Tap *Continue* to access the bot.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

    else:

        keyboard = [
            [
                InlineKeyboardButton(
                    "📢 Join Telegram Channel",
                    url="https://t.me/Vickyupdatemayor"
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
            "❌ You haven't joined the Telegram channel yet.\n\n"
            "Please join the channel and then tap *Check Again*.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def continue_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    await query.message.edit_text(
        "🎉 *Welcome!*\n\n"
        "You have passed the join requirements.\n\n"
        "Your bot is now ready to use.",
        parse_mode="Markdown"
    )


def main():

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing.")

    threading.Thread(
        target=run_server,
        daemon=True
    ).start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CallbackQueryHandler(check_membership, pattern="^check$")
    )
    application.add_handler(
        CallbackQueryHandler(continue_bot, pattern="^continue$")
    )

    print("Vicky Join Bot started!")

    application.run_polling()


if __name__ == "__main__":
    main()
