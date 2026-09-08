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
        parse_mode="Markdown",
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
            parse_mode="Markdown",
        )
        return

    rewarded = reward_referrer(user_id)
    text = "âœ… *Membership verified!\n\nWelcome to Freecash_bot.*"
    if rewarded:
        text += "\n\nðŸŽ Your referrer has received â‚¦100."
    await query.message.edit_text(text, reply_markup=menu_markup(), parse_mode="Markdown")


async def continue_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("ðŸ  *Main Menu*", reply_markup=menu_markup(), parse_mode="Markdown")


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, refs = user_stats(query.from_user.id)
    await query.message.edit_text(
        f"ðŸ‘¥ *My Referrals*\n\nSuccessful referrals: *{refs}*\nEarned: *â‚¦{refs * REFERRAL_REWARD:,}*\nBalance: *â‚¦{bal:,}*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
        parse_mode="Markdown",
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, refs = user_stats(query.from_user.id)
    await query.message.edit_text(
        f"ðŸ’° *Balance*\n\nAvailable: *â‚¦{bal:,}*\nReferrals: *{refs}*\nMinimum withdrawal: *â‚¦{MIN_WITHDRAWAL:,}*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
        parse_mode="Markdown",
    )


async def ref_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={query.from_user.id}"
    await query.message.edit_text(
        f"ðŸ”— *Your Referral Link*\n\n`{link}`\n\nEarn *â‚¦{REFERRAL_REWARD}* for each successful referral.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
        parse_mode="Markdown",
    )


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bal, _ = user_stats(query.from_user.id)
    if bal < MIN_WITHDRAWAL:
        await query.message.edit_text(
            f"ðŸ’¸ *Withdrawal*\n\nBalance: *â‚¦{bal:,}*\nMinimum: *â‚¦{MIN_WITHDRAWAL:,}*\n\nYou need *â‚¦{MIN_WITHDRAWAL-bal:,}* more.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Back", callback_data="menu")]]),
            parse_mode="Markdown",
        )
        return
    context.user_data["withdraw_step"] = "details"
    await query.message.edit_text(
        "ðŸ’¸ *Withdrawal Request*\n\nSend your bank details in exactly this format:\n\n`Bank Name | Account Number | Account Name`\n\nExample:\n`GTBank | 0123456789 | John Doe`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("âŒ Cancel", callback_data="menu")]]),
        parse_mode="Markdown",
    )


async def bank_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("withdraw_step") != "details":
        return
    raw = (update.message.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not all(parts):
        await update.message.reply_text("âŒ Invalid format. Use:\n`Bank Name | Account Number | Account Name`", parse_mode="Markdown")
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
        parse_mode="Markdown",
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"ðŸ’¸ *NEW WITHDRAWAL #{wid}*\n\nUser: `{update.effective_user.id}`\nAmount: *â‚¦{bal:,}*\nBank: *{bank}*\nAccount: `{account}`\nName: *{name}*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("âœ… Approve", callback_data=f"approve:{wid}"), InlineKeyboardButton("âŒ Reject + Refund", callback_data=f"reject:{wid}")]
            ]),
            parse_mode="Markdown",
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
        await update.callback_query.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


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
        await query.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Admin", callback_data="a_home")]]), parse_mode="Markdown")
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
            text = "ðŸ’¸ *Pending Withdrawals*\n\nSelect a    with db_lock:
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
