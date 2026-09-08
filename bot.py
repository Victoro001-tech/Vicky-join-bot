import os
import threading

import psycopg2
from flask import Flask, Response
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# SETTINGS
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = 6225743234
TELEGRAM_CHANNEL = "@Vickyupdatemayor"
TELEGRAM_LINK = "https://t.me/Vickyupdatemayor"
WHATSAPP_LINK = "https://whatsapp.com/channel/0029VbDyRS18F2p6910NlS1j"
REFERRAL_REWARD = 100
MIN_WITHDRAWAL = 500

DB_LOCK = threading.RLock()
app_web = Flask(__name__)


# =========================
# DATABASE HELPERS
# =========================
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, connect_timeout=15)


def init_db():
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" CREATE TABLE IF NOT EXISTS users ( user_id BIGINT PRIMARY KEY, username TEXT, balance BIGINT NOT NULL DEFAULT 0, referrals INTEGER NOT NULL DEFAULT 0, referred_by BIGINT, referral_rewarded INTEGER NOT NULL DEFAULT 0 ) """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS balance BIGINT NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrals INTEGER NOT NULL DEFAULT 0")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by BIGINT")

                cur.execute(""" DO $$ BEGIN IF EXISTS ( SELECT 1 FROM information_schema.columns WHERE table_name='users' AND column_name='referral_rewarded' AND data_type='boolean' ) THEN ALTER TABLE users ALTER COLUMN referral_rewarded TYPE INTEGER USING CASE WHEN referral_rewarded THEN 1 ELSE 0 END; END IF; END $$; """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded INTEGER NOT NULL DEFAULT 0")

                cur.execute(""" CREATE TABLE IF NOT EXISTS withdrawals ( id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT, amount BIGINT NOT NULL, bank_name TEXT NOT NULL DEFAULT '', account_number TEXT NOT NULL DEFAULT '', account_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ) """)
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS username TEXT")
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS bank_name TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS account_number TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'")
                cur.execute("ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                cur.execute("CREATE INDEX IF NOT EXISTS withdrawals_status_idx ON withdrawals(status)")
            conn.commit()
            print("Database ready")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def ensure_user(user_id, username):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = COALESCE(EXCLUDED.username, users.username) """, (user_id, username))
            conn.commit()
        finally:
            conn.close()


def set_referrer(user_id, referrer_id):
    if user_id == referrer_id:
        return False
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" UPDATE users SET referred_by = %s WHERE user_id = %s AND referred_by IS NULL AND EXISTS (SELECT 1 FROM users WHERE user_id = %s) """, (referrer_id, user_id, referrer_id))
                changed = cur.rowcount == 1
            conn.commit()
            return changed
        finally:
            conn.close()


def reward_referrer(user_id):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" SELECT referred_by, referral_rewarded FROM users WHERE user_id=%s FOR UPDATE """, (user_id,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False
                referrer_id, rewarded = row
                if not referrer_id or int(rewarded or 0) == 1:
                    conn.rollback()
                    return False
                cur.execute(""" UPDATE users SET balance = balance + %s, referrals = referrals + 1 WHERE user_id=%s """, (REFERRAL_REWARD, referrer_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return False
                cur.execute("UPDATE users SET referral_rewarded=1 WHERE user_id=%s", (user_id,))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def user_stats(user_id):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT balance, referrals FROM users WHERE user_id=%s", (user_id,))
                return cur.fetchone() or (0, 0)
        finally:
            conn.close()


def create_withdrawal(user_id, username, amount, bank, account, name):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE", (user_id,))
                row = cur.fetchone()
                if not row or int(row[0]) < amount:
                    conn.rollback()
                    return None
                cur.execute("UPDATE users SET balance=balance-%s WHERE user_id=%s", (amount, user_id))
                cur.execute(""" INSERT INTO withdrawals (user_id, username, amount, bank_name, account_number, account_name, status) VALUES (%s,%s,%s,%s,%s,%s,'pending') RETURNING id """, (user_id, username, amount, bank, account, name))
                wid = cur.fetchone()[0]
            conn.commit()
            return wid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_withdrawal(wid):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" SELECT id,user_id,username,amount,bank_name,account_number,account_name,status,created_at FROM withdrawals WHERE id=%s """, (wid,))
                return cur.fetchone()
        finally:
            conn.close()


def pending_withdrawals(limit=20):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" SELECT id,user_id,username,amount,bank_name,account_number,account_name,status,created_at FROM withdrawals WHERE status='pending' ORDER BY id ASC LIMIT %s """, (limit,))
                return cur.fetchall()
        finally:
            conn.close()


def approve_withdrawal(wid):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" UPDATE withdrawals SET status='approved' WHERE id=%s AND status='pending' RETURNING user_id,amount """, (wid,))
                row = cur.fetchone()
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def reject_withdrawal(wid):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(""" SELECT user_id,amount FROM withdrawals WHERE id=%s AND status='pending' FOR UPDATE """, (wid,))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                user_id, amount = row
                cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, user_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return None
                cur.execute("UPDATE withdrawals SET status='rejected' WHERE id=%s AND status='pending'", (wid,))
            conn.commit()
            return user_id, amount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def admin_stats():
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*),COALESCE(SUM(balance),0),COALESCE(SUM(referrals),0) FROM users")
                users, balance, referrals = cur.fetchone()
                cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")
                pending = cur.fetchone()[0]
                return users, balance, referrals, pending
        finally:
            conn.close()


def latest_users(limit=15):
    with DB_LOCK:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id,username,balance,referrals FROM users ORDER BY user_id DESC LIMIT %s", (limit,))
                return cur.fetchall()
        finally:
            conn.close()


# =========================
# WEB HEALTH CHECK
# =========================
@app_web.route("/")
def home():
    return "Vicky Join Bot is running", 200


@app_web.route("/health")
def health():
    return "OK", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))
    app_web.run(host="0.0.0.0", port=port, use_reloader=False)


# =========================
# TELEGRAM UI
# =========================
def admin(user_id):
    return int(user_id) == ADMIN_ID


def join_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ“¢ Join Telegram Channel", url=TELEGRAM_LINK)],
        [InlineKeyboardButton("ðŸ“± Join WhatsApp Channel", url=WHATSAPP_LINK)],
        [InlineKeyboardButton("âœ… I've Joined", callback_data="check_join")],
    ])


def menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ‘¥ My Referrals", callback_data="referrals"), InlineKeyboardButton("ðŸ’° Balance", callback_data="balance")],
        [InlineKeyboardButton("ðŸ”— Referral Link", callback_data="ref_link"), InlineKeyboardButton("ðŸ’¸ Withdraw", callback_data="withdraw")],
    ])


def admin_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ðŸ“Š Stats", callback_data="a_stats"), InlineKeyboardButton("ðŸ‘¥ Users", callback_data="a_users")],
        [InlineKeyboardButton("ðŸ’¸ Pending Withdrawals", callback_data="a_wds")],
        [InlineKeyboardButton("ðŸ”„ Refresh", callback_data="a_home")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)
    context.user_data.pop("withdraw_step", None)
    context.user_data.pop("withdraw_data", None)

    if context.args:
        try:
            referrer = int(context.args[0])
            set_referrer(user.id, referrer)
        except (ValueError, TypeError):
            pass

    await update.message.reply_text(
        "ðŸŽ‰ *Welcome to Freecash_bot!\n\nJoin both channels, then press* âœ… *I've Joined*.\n\nYou earn â‚¦100 for every successful referral.*",
        reply_markup=join_markup(),
    )


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        member = await context.bot.get_chat_member(TELEGRAM_CHANNEL, user_id)
        allowed = member.status in ("member", "administrator", "creator")
    except Exception as exc:
        print("Membership check error:", exc)
        allowed = False

    if not allowed:
        await query.message.edit_text(
            "âŒ *Telegram membership was not detected yet.*\n\nPlease join the Telegram channel first, then press the button again.",
            reply_markup=join_markup(),
            )
        return

    rewarded = reward_referrer(user_id)
    text = "âœ… *Membership verified!\n\nWelcome to Freecash_bot.*"
    if rewarded:
        text += "\n\nðŸŽ Your referrer has received â‚¦100."
    await query.message.edit_text(text, reply_markup=menu_markup())


async def continue_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("ðŸ  *Main Menu*", reply_markup=menu_markup())


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, refs = user_stats(query.from_user.id)
    await query.message.edit_text(
        f"ðŸ‘¥ *My Referrals*\n\nSuccessful referrals: *{refs}*\nEarned: *â‚¦{refs * REFERRAL_REWARD:,}*\nBalance: *â‚¦{bal:,}*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, refs = user_stats(query.from_user.id)
    await query.message.edit_text(
        f"ðŸ’° *Balance*\n\nAvailable: *â‚¦{bal:,}*\nReferrals: *{refs}*\nMinimum withdrawal: *â‚¦{MIN_WITHDRAWAL:,}*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
    )


async def ref_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={query.from_user.id}"
    await query.message.edit_text(
        f"ðŸ”— *Your Referral Link*\n\n`{link}`\n\nEarn *â‚¦{REFERRAL_REWARD}* for each successful referral.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
    )


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, _ = user_stats(query.from_user.id)
    if bal < MIN_WITHDRAWAL:
        await query.message.edit_text(
            f"ðŸ’¸ *Withdrawal*\n\nBalance: *â‚¦{bal:,}*\nMinimum: *â‚¦{MIN_WITHDRAWAL:,}*\n\nYou need *â‚¦{MIN_WITHDRAWAL-bal:,}* more.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
            )
        return
    context.user_data["withdraw_step"] = "details"
    await query.message.edit_text(
        "ðŸ’¸ *Withdrawal Request*\n\nSend your bank details in exactly this format:\n\n`Bank Name | Account Number | Account Name`\n\nExample:\n`GTBank | 0123456789 | John Doe`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("âŒ Cancel", callback_data="menu")]]),
    )


async def bank_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("withdraw_step") != "details":
        return
    raw = (update.message.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        await update.message.reply_text("âŒ Invalid format. Use:\n`Bank Name | Account Number | Account Name`")
        return
    bank, account, name = parts
    if not account.isdigit() or not (8 <= len(account) <= 12):
        await update.message.reply_text("âŒ Account number should contain 8â€“12 digits.")
        return

    bal, _ = user_stats(update.effective_user.id)
    if bal < MIN_WITHDRAWAL:
        context.user_data.clear()
        await update.message.reply_text("âŒ Your balance is now below the minimum withdrawal amount.")
        return

    wid = create_withdrawal(update.effective_user.id, update.effective_user.username, bal, bank, account, name)
    context.user_data.clear()
    if not wid:
        await update.message.reply_text("âŒ Withdrawal could not be created. Please try again.")
        return

    await update.message.reply_text(
        f"âœ… *Withdrawal submitted!*\n\nRequest: *#{wid}*\nAmount: *â‚¦{bal:,}*\nBank: *{bank}*\nAccount: `{account}`\nName: *{name}*\n\nYour request is pending admin approval.",
        reply_markup=menu_markup(),
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"ðŸ’¸ *NEW WITHDRAWAL #{wid}*\n\nUser: `{update.effective_user.id}`\nAmount: *â‚¦{bal:,}*\nBank: *{bank}*\nAccount: `{account}`\nName: *{name}*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("âœ… Approve", callback_data=f"approve:{wid}"), InlineKeyboardButton("âŒ Reject + Refund", callback_data=f"reject:{wid}")]
            ]),
            )
    except Exception as exc:
        print("Admin notification error:", exc)


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        await update.message.reply_text("â›” Admin only.")
        return
    await show_admin(update, context)


async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users, total_balance, referrals_count, pending = admin_stats()
    text = (
        "ðŸ›  *Admin Panel*\n\n"
        f"ðŸ‘¥ Users: *{users:,}*\n"
        f"ðŸ’° Total balances: *â‚¦{total_balance:,}*\n"
        f"ðŸ”— Referrals: *{referrals_count:,}*\n"
        f"ðŸ’¸ Pending withdrawals: *{pending:,}*"
    )
    markup = admin_markup()
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not admin(query.from_user.id):
        return
    data = query.data

    if data == "a_home":
        await show_admin(update, context)
        return
    if data == "a_stats":
        await show_admin(update, context)
        return
    if data == "a_users":
        rows = latest_users()
        lines = ["ðŸ‘¥ *Latest Users*", ""]
        for uid, username, bal, refs in rows:
            lines.append(f"`{uid}` {('@'+username) if username else 'No username'}\nâ‚¦{bal:,} | {refs} referrals")
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Admin", callback_data="a_home")]]))
        return
    if data == "a_wds":
        rows = pending_withdrawals()
        if not rows:
            text = "ðŸ’¸ *Pending Withdrawals*\n\nNone."
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Admin", callback_data="a_home")]])
        else:
            buttons = []
            for row in rows:
                wid, uid, username, amount, bank, account, name, status, created = row
                buttons.append([InlineKeyboardButton(f"#{wid} â€” â‚¦{amount:,}", callback_data=f"view:{wid}")])
            buttons.append([InlineKeyboardButton("ðŸ”™ Admin", callback_data="a_home")])
            text = "ðŸ’¸ *Pending Withdrawals*\n\nSelect a request:"
            markup = InlineKeyboardMarkup(buttons)
        await query.message.edit_text(text, reply_markup=markup)
        return
    if data.startswith("view:"):
        wid = int(data.split(":", 1)[1])
        row = get_withdrawal(wid)
        if not row:
            await query.answer("Not found", show_alert=True)
            return
        wid, uid, username, amount, bank, account, name, status, created = row
        text = (f"ðŸ’¸ *Withdrawal #{wid}*\n\nUser: `{uid}`\nAmount: *â‚¦{amount:,}*\nBank: *{bank}*\nAccount: `{account}`\nName: *{name}*\nStatus: *{status}*")
        buttons = []
        if status == "pending":
            buttons.append([InlineKeyboardButton("âœ… Approve", callback_data=f"approve:{wid}"), InlineKeyboardButton("âŒ Reject + Refund", callback_data=f"reject:{wid}")])
        buttons.append([InlineKeyboardButton("ðŸ”™ Pending", callback_data="a_wds")])
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        return
    if data.startswith("approve:"):
        wid = int(data.split(":", 1)[1])
        row = approve_withdrawal(wid)
        if not row:
            await query.answer("Already processed or not found", show_alert=True)
            return
        uid, amount = row
        await query.message.edit_text(f"âœ… *Withdrawal #{wid} approved.*\n\nAmount: â‚¦{amount:,}\nUser: `{uid}`", reply_markup=admin_markup())
        try:
            await context.bot.send_message(uid, f"âœ… Your withdrawal *#{wid}* of *â‚¦{amount:,}* has been approved.")
        except Exception as exc:
            print("User approval notification error:", exc)
        return
    if data.startswith("reject:"):
        wid = int(data.split(":", 1)[1])
        row = reject_withdrawal(wid)
        if not row:
            await query.answer("Already processed or not found", show_alert=True)
            return
        uid, amount = row
        await query.message.edit_text(f"âŒ *Withdrawal #{wid} rejected and refunded.*\n\nRefund: â‚¦{amount:,}\nUser: `{uid}`", reply_markup=admin_markup())
        try:
            await context.bot.send_message(uid, f"âŒ Your withdrawal *#{wid}* was rejected. â‚¦{amount:,} has been refunded to your balance.")
        except Exception as exc:
            print("User refund notification error:", exc)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /start to open the bot. Use /admin for the admin panel.")


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("admin", "Admin panel"),
        BotCommand("help", "Help"),
    ])


# =========================
# STARTUP
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    init_db()
    threading.Thread(target=run_web, daemon=True).start()

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(check_join, pattern=r"^check_join$"))
    application.add_handler(CallbackQueryHandler(referrals, pattern=r"^referrals$"))
    application.add_handler(CallbackQueryHandler(balance, pattern=r"^balance$"))
    application.add_handler(CallbackQueryHandler(ref_link, pattern=r"^ref_link$"))
    application.add_handler(CallbackQueryHandler(withdraw, pattern=r"^withdraw$"))
    application.add_handler(CallbackQueryHandler(continue_menu, pattern=r"^menu$"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^(a_home|a_stats|a_users|a_wds|view:\d+|approve:\d+|reject:\d+)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bank_details))

    print("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
