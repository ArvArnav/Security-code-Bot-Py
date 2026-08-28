import asyncio
import os
import json
import hmac
import hashlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl
import logging
import re
import secrets
import string
import sqlite3
from datetime import datetime
from html import escape
from urllib.parse import quote

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("8591449341:AAGrn2wZq9CUxFdmKShOPHVirvQkCPfgC9A", "")
ADMIN_CHAT_ID = 6982378210

BOT_DEVELOPER = "Ꭰᴇɴᴠᴇʀ Ꭰᴀꜱ ⚡️"
BOT_OWNER = "@DenverDas"
CHANNEL_1 = "https://t.me/DenversEra"
CHANNEL_2 = "https://t.me/DenverBackup"

DB_FILE = "requests.db"
UPI_ID = "gabbarxsingh@fam"
UPI_NAME = "Arnav Singh"


def payment_qr_url(amount_text):
    """Return a hosted QR image URL for this exact UPI payment amount."""
    amount = amount_text.replace("₹", "").replace(",", "").strip()
    upi_uri = f"upi://pay?pa={UPI_ID}&pn={UPI_NAME}&am={amount}&cu=INR"
    return (
        "https://api.qrserver.com/v1/create-qr-code/?size=700x700&format=jpg"
        f"&data={quote(upi_uri, safe='')}"
    )

# =========================
# COIN SYSTEM CONFIG
# =========================
SECURITY_CODE_COST = 10

# User-facing coin packages. Format: package_id -> (coins, display_price)
COIN_PACKAGES = {
    "10": (10, "₹249"),
    "25": (25, "₹498"),
    "50": (50, "₹1,245"),
    "100": (100, "₹2,490"),
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================
# DATABASE
# =========================
def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    con = db()

    # Existing requests table from older versions is kept intact.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            username TEXT,
            token_value TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # Migrate an older database that used token_value instead of token_value.
    cols = {row[1] for row in con.execute("PRAGMA table_info(requests)").fetchall()}
    if "token_value" not in cols:
        con.execute("ALTER TABLE requests ADD COLUMN token_value TEXT")
        if "token_value" in cols:
            con.execute(
                "UPDATE requests SET token_value = token_value "
                "WHERE token_value IS NULL"
            )

    # User wallet.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT,
            coins INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    # Coin purchase/top-up requests.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS coin_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            username TEXT,
            coins INTEGER NOT NULL,
            price TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    # Payment screenshots are stored as Telegram file IDs, not local files.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS coin_payment_proofs (
            request_id INTEGER PRIMARY KEY,
            file_id TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
        """
    )

    con.commit()
    con.close()


def ensure_user(user):
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    con.execute(
        """
        INSERT INTO users (user_id, name, username, coins, created_at, updated_at)
        VALUES (?, ?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username,
            updated_at = excluded.updated_at
        """,
        (user.id, user.full_name, user.username or "", now, now),
    )
    con.commit()
    con.close()


def is_admin_user(user_id):
    return user_id == ADMIN_CHAT_ID


def display_balance(user_id):
    return "∞ UNLIMITED" if is_admin_user(user_id) else str(get_balance(user_id))


def get_balance(user_id):
    con = db()
    row = con.execute(
        "SELECT coins FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    con.close()
    return row[0] if row else 0


def add_coins(user_id, amount):
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    cur = con.execute(
        "UPDATE users SET coins = coins + ?, updated_at = ? WHERE user_id = ?",
        (amount, now, user_id),
    )
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed


def remove_coins(user_id, amount):
    """Remove coins atomically without allowing the balance to go negative."""
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    cur = con.execute(
        """
        UPDATE users
        SET coins = coins - ?, updated_at = ?
        WHERE user_id = ? AND coins >= ?
        """,
        (amount, now, user_id, amount),
    )
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed


def spend_coins(user_id, amount):
    # Admin wallet is unlimited and is never decremented.
    if is_admin_user(user_id):
        return True
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    cur = con.execute(
        """
        UPDATE users
        SET coins = coins - ?, updated_at = ?
        WHERE user_id = ? AND coins >= ?
        """,
        (amount, now, user_id, amount),
    )
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed


def create_coin_request(user, coins, price):
    ensure_user(user)
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    cur = con.execute(
        """
        INSERT INTO coin_requests
        (user_id, name, username, coins, price, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            user.full_name,
            user.username or "",
            coins,
            price,
            "PENDING",
            now,
        ),
    )
    request_id = cur.lastrowid
    con.commit()
    con.close()
    return request_id


def get_coin_request(request_id):
    con = db()
    row = con.execute(
        "SELECT * FROM coin_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    con.close()
    return row


def set_coin_request_status(request_id, status):
    con = db()
    con.execute(
        "UPDATE coin_requests SET status = ? WHERE id = ?",
        (status, request_id),
    )
    con.commit()
    con.close()


def save_payment_proof(request_id, file_id):
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    con = db()
    con.execute(
        """
        INSERT INTO coin_payment_proofs (request_id, file_id, received_at)
        VALUES (?, ?, ?)
        ON CONFLICT(request_id) DO UPDATE SET
            file_id = excluded.file_id,
            received_at = excluded.received_at
        """,
        (request_id, file_id, now),
    )
    con.commit()
    con.close()


def has_payment_proof(request_id):
    con = db()
    row = con.execute(
        "SELECT 1 FROM coin_payment_proofs WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    con.close()
    return row is not None


def delete_payment_proof(request_id):
    con = db()
    con.execute("DELETE FROM coin_payment_proofs WHERE request_id = ?", (request_id,))
    con.commit()
    con.close()


def create_request(user, token_value):
    ensure_user(user)
    con = db()
    cur = con.execute(
        """
        INSERT INTO requests
        (user_id, name, username, token_value, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user.id,
            user.full_name,
            user.username or "",
            token_value,
            "PENDING",
            datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        ),
    )
    request_id = cur.lastrowid
    con.commit()
    con.close()
    return request_id


def get_request(request_id):
    con = db()
    # Explicit columns keep this compatible with older databases that may
    # still contain the legacy token_value column.
    row = con.execute(
        """
        SELECT id, user_id, name, username, token_value, status, created_at
        FROM requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()
    con.close()
    return row


def set_status(request_id, status):
    con = db()
    con.execute(
        "UPDATE requests SET status = ? WHERE id = ?",
        (status, request_id),
    )
    con.commit()
    con.close()


def counts():
    con = db()
    rows = con.execute(
        "SELECT status, COUNT(*) FROM requests GROUP BY status"
    ).fetchall()
    con.close()

    result = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
    for status, count in rows:
        result[status] = count
    return result


def coin_counts():
    con = db()
    rows = con.execute(
        "SELECT status, COUNT(*) FROM coin_requests GROUP BY status"
    ).fetchall()
    con.close()

    result = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
    for status, count in rows:
        result[status] = count
    return result


def get_all_users():
    con = db()
    rows = con.execute(
        """
        SELECT user_id, name, username, coins, created_at, updated_at
        FROM users
        WHERE user_id != ?
        ORDER BY updated_at DESC
        """,
        (ADMIN_CHAT_ID,),
    ).fetchall()
    con.close()
    return rows


def get_user_purchase_stats(user_id):
    con = db()
    row = con.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'APPROVED' THEN coins ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'REJECTED' THEN 1 ELSE 0 END), 0)
        FROM coin_requests
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    con.close()
    return row or (0, 0, 0, 0)


def get_user_full_history(user_id):
    """Return both coin-purchase history and submitted access/security tokens."""
    con = db()
    coin_rows = con.execute(
        """
        SELECT id, 'COIN' AS kind, coins, price, status, created_at, NULL AS token_value
        FROM coin_requests
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    token_rows = con.execute(
        """
        SELECT id, 'TOKEN' AS kind, NULL AS coins, NULL AS price, status, created_at, token_value
        FROM requests
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchall()
    con.close()
    return sorted(coin_rows + token_rows, key=lambda row: row[5], reverse=True)


def admin_users_keyboard(users):
    rows = []
    for user_id, name, username, coins, created_at, updated_at in users[:40]:
        label_name = (name or "Unknown").replace("\n", " ")[:22]
        rows.append([
            InlineKeyboardButton(
                f"👤 {label_name} • 🪙 {coins}",
                callback_data=f"adminuser:{user_id}",
            )
        ])
    rows.append([InlineKeyboardButton("🔄 REFRESH USERS", callback_data="adminusers:0")])
    rows.append([InlineKeyboardButton("⬅️ ADMIN PANEL", callback_data="adminpanel:0")])
    return InlineKeyboardMarkup(rows)


def admin_user_actions_keyboard(user_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ ADD COINS", callback_data=f"adminadd:{user_id}"),
                InlineKeyboardButton("➖ REMOVE COINS", callback_data=f"adminremove:{user_id}"),
            ],
            [InlineKeyboardButton("👤 USER INFO", callback_data=f"adminuserinfo:{user_id}")],
            [InlineKeyboardButton("🧾 PURCHASE HISTORY", callback_data=f"adminhistory:{user_id}")],
            [InlineKeyboardButton("⬅️ ALL USERS", callback_data="adminusers:0")],
        ]
    )


def admin_panel_keyboard(request_id=None):
    rows = [
        [InlineKeyboardButton("👥 ALL USERS & HISTORY", callback_data="adminusers:0")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="adminbroadcast:0")],
    ]
    if request_id is not None:
        rows += [
            [InlineKeyboardButton("👤 USER INFO", callback_data=f"coinuser:{request_id}")],
            [
                InlineKeyboardButton("➕ ADD COINS", callback_data=f"adminaddrequest:{request_id}"),
                InlineKeyboardButton("➖ REMOVE COINS", callback_data=f"coinremove:{request_id}"),
            ],
            [InlineKeyboardButton("⬅️ BACK TO REQUEST", callback_data=f"backcoin:{request_id}")],
        ]
    return InlineKeyboardMarkup(rows)


def admin_amount_keyboard(user_id, mode):
    prefix = "adminaddamount" if mode == "add" else "adminremoveamount"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ 1" if mode == "add" else "➖ 1", callback_data=f"{prefix}:{user_id}:1"),
                InlineKeyboardButton("➕ 5" if mode == "add" else "➖ 5", callback_data=f"{prefix}:{user_id}:5"),
                InlineKeyboardButton("➕ 10" if mode == "add" else "➖ 10", callback_data=f"{prefix}:{user_id}:10"),
            ],
            [
                InlineKeyboardButton("➕ 25" if mode == "add" else "➖ 25", callback_data=f"{prefix}:{user_id}:25"),
                InlineKeyboardButton("➕ 50" if mode == "add" else "➖ 50", callback_data=f"{prefix}:{user_id}:50"),
                InlineKeyboardButton("➕ 100" if mode == "add" else "➖ 100", callback_data=f"{prefix}:{user_id}:100"),
            ],
            [InlineKeyboardButton("✏️ CUSTOM AMOUNT", callback_data=f"admincustom:{user_id}:{mode}")],
            [InlineKeyboardButton("⬅️ USER PANEL", callback_data=f"adminuser:{user_id}")],
        ]
    )


# =========================
# TOKEN VALIDATION
# =========================
def is_access_token(value):
    # 50 or more lowercase alphanumeric characters.
    return bool(re.fullmatch(r"[0-9a-z]{50,}", value))


def random_security_code():
    return "".join(secrets.choice(string.digits) for _ in range(6))


def verification_id():
    alphabet = string.ascii_uppercase + string.digits
    return "VR-" + "".join(secrets.choice(alphabet) for _ in range(8))


# =========================
# UI
# =========================
def fixed_keyboard(is_admin=False):
    rows = [
        ["🔎 FIND SECURITY CODE", "🪙 MY COINS"],
        ["💰 BUY COINS", "🧾 VERIFICATION INFO"],
        ["📢 CHANNELS", "ℹ️ ABOUT"],
        ["🛠️ SUPPORT", "🏠 MAIN MENU"],
    ]
    if is_admin:
        rows.append(["🛠️ ADMIN PANEL"])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option…",
    )




def menu_keyboard(user_id):
    return fixed_keyboard(user_id == ADMIN_CHAT_ID)

def admin_panel(request_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ APPROVE",
                    callback_data=f"approve:{request_id}",
                ),
                InlineKeyboardButton(
                    "❌ REJECT",
                    callback_data=f"reject:{request_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👤 USER INFO",
                    callback_data=f"user:{request_id}",
                ),
                InlineKeyboardButton(
                    "🔄 REFRESH",
                    callback_data=f"refresh:{request_id}",
                ),
            ],
        ]
    )


def coin_package_keyboard():
    rows = []
    for package_id, (coins, price) in COIN_PACKAGES.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"🪙 {coins} COINS • {price}",
                    callback_data=f"buycoins:{package_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("❌ CLOSE", callback_data="close:0")]
    )
    return InlineKeyboardMarkup(rows)


def coin_admin_panel(request_id, status="PENDING"):
    rows = []
    if status == "PENDING":
        rows.append([
            InlineKeyboardButton("✅ ADD COINS", callback_data=f"coinapprove:{request_id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"coinreject:{request_id}"),
        ])

    # Open the user's admin controls directly.  Using the user-id callback
    # avoids routing the button through the request dashboard and makes the
    # post-approval button reliable even after the coin request is closed.
    row = get_coin_request(request_id)
    if row:
        user_id = row[1]
        rows.append([
            InlineKeyboardButton("🛠️ ADMIN PANEL", callback_data=f"adminuser:{user_id}")
        ])
    else:
        rows.append([InlineKeyboardButton("🛠️ ADMIN PANEL", callback_data="adminpanel:0")])

    return InlineKeyboardMarkup(rows)


def remove_coins_keyboard(user_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖ 1", callback_data=f"removecoins:{user_id}:1"),
                InlineKeyboardButton("➖ 5", callback_data=f"removecoins:{user_id}:5"),
                InlineKeyboardButton("➖ 10", callback_data=f"removecoins:{user_id}:10"),
            ],
            [
                InlineKeyboardButton("➖ 25", callback_data=f"removecoins:{user_id}:25"),
                InlineKeyboardButton("➖ 50", callback_data=f"removecoins:{user_id}:50"),
                InlineKeyboardButton("➖ 100", callback_data=f"removecoins:{user_id}:100"),
            ],
        ]
    )


# =========================
# USER HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    balance = get_balance(user.id)

    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🔐 <b>SECURITY VAULT</b>   ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "⚡ <b>Fast & Secure Verification</b>\n"
        "🔑 <b>Access Token Authentication</b>\n"
        "🪙 <b>Coin Based Security Code</b>\n\n"
        f"💰 Your Balance: <b>{display_balance(user.id)} COINS</b>\n"
        f"🔐 Code Cost: <b>{SECURITY_CODE_COST} COIN</b>\n\n"
        "✨ <b>Select an option below</b> 👇",
        parse_mode="HTML",
        reply_markup=menu_keyboard(update.effective_user.id),
    )


async def show_coins(update, context):
    user = update.effective_user
    ensure_user(user)
    balance = get_balance(user.id)

    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   🪙 <b>MY COINS</b>   ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"💰 Current Balance: <b>{display_balance(user.id)} COINS</b>\n"
        f"🔐 Security Code Cost: <b>{SECURITY_CODE_COST} COIN</b>\n\n"
        "💡 Buy more coins anytime from <b>💰 BUY COINS</b>.",
        parse_mode="HTML",
        reply_markup=menu_keyboard(update.effective_user.id),
    )


async def show_buy_coins(update, context):
    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   💰 <b>BUY COINS</b>   ✦\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🪙 Select a coin package below.\n"
        "📩 Your purchase request will be sent to the admin.\n"
        "✅ After admin approval, coins are added to your balance.\n\n"
        "👇 <b>Choose your package:</b>",
        parse_mode="HTML",
        reply_markup=coin_package_keyboard(),
    )


async def keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "🛠️ ADMIN PANEL":
        if update.effective_user.id != ADMIN_CHAT_ID:
            await update.message.reply_text("⛔ Admin only.", reply_markup=menu_keyboard(update.effective_user.id))
            return
        c = counts()
        cc = coin_counts()
        users = len(get_all_users())
        await update.message.reply_text(
            "🛠️ <b>ADMIN PANEL</b>\n\n"
            f"👥 Total Users: <b>{users}</b>\n"
            f"⏳ Token Pending: <b>{c['PENDING']}</b>\n"
            f"🪙 Coin Pending: <b>{cc['PENDING']}</b>\n\n"
            "Choose an admin action below:",
            parse_mode="HTML",
            reply_markup=admin_panel_keyboard(),
        )
        return

    if text == "🔎 FIND SECURITY CODE":
        user = update.effective_user
        ensure_user(user)
        balance = get_balance(user.id)

        if not is_admin_user(user.id) and balance < SECURITY_CODE_COST:
            await update.message.reply_text(
                "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                "   🪙 <b>INSUFFICIENT COINS</b>   ✦\n"
                "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"💰 Your Balance: <b>{display_balance(user.id)} COINS</b>\n"
                f"🔐 Required: <b>{SECURITY_CODE_COST} COIN</b>\n\n"
                "👉 Please use <b>💰 BUY COINS</b> to request more coins from admin.",
                parse_mode="HTML",
                reply_markup=menu_keyboard(update.effective_user.id),
            )
            return

        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🔎 <b>TOKEN SCANNER</b>   ⚡\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🧪 <b>Access Token Example:</b>\n"
            "<code>c561c00989f48b269517ec95dba85ef14098eeed1008d31a7ba2890895f2d5a6</code>\n\n"
            "📏 Minimum Length: <b>50 Characters</b>\n"
            "🔤 Allowed: <b>a-z + 0-9</b>\n"
            f"🪙 Cost: <b>{SECURITY_CODE_COST} COIN</b>\n\n"
            "✨ <b>Send your valid Access Token below…</b>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    elif text == "🪙 MY COINS":
        await show_coins(update, context)

    elif text == "💰 BUY COINS":
        await show_buy_coins(update, context)

    elif text == "🧾 VERIFICATION INFO":
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🧾 <b>VERIFICATION INFO</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "📏 Token Length: <b>50+</b>\n"
            "🔤 Allowed: <b>a-z + 0-9</b>\n"
            "🔡 Uppercase: ❌\n"
            "🔣 Special Characters: ❌\n"
            "🪙 Security Code Cost: "
            f"<b>{SECURITY_CODE_COST} COIN</b>\n\n"
            "⚡ <i>Secure • Fast • Automated</i>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    elif text == "📢 CHANNELS":
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   📢 <b>OFFICIAL CHANNELS</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🔷 <b>MAIN CHANNEL</b>\n"
            f"└➤ {escape(CHANNEL_1)}\n\n"
            "🔹 <b>BACKUP CHANNEL</b>\n"
            f"└➤ {escape(CHANNEL_2)}\n\n"
            "✨ <i>Stay connected for latest updates!</i>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    elif text == "ℹ️ ABOUT":
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ℹ️ <b>ABOUT THE BOT</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🤖 <b>ACCESS TOKEN SECURITY BOT</b>\n\n"
            "⚡ Fast & Easy Verification\n"
            "🔐 Secure Token Validation\n"
            "🪙 Coin Based Security Code\n"
            "🧾 Verification Information\n"
            "🛡️ Automated Security System\n\n"
            f"👨‍💻 <b>Developer:</b> {escape(BOT_DEVELOPER)}\n"
            f"👑 <b>Owner:</b> {escape(BOT_OWNER)}\n\n"
            "✨ <i>Secure • Fast • Reliable</i>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    elif text == "🛠️ SUPPORT":
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🛠️ <b>SUPPORT CENTER</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "💬 <b>Need Help?</b>\n\n"
            "⚡ If you're facing any issue with verification,\n"
            "feel free to contact the owner.\n\n"
            f"👑 <b>Owner:</b> {escape(BOT_OWNER)}\n\n"
            "🔔 <i>Our support team will assist you.</i>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    elif text == "🏠 MAIN MENU":
        await start(update, context)


async def receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user = update.effective_user
    ensure_user(user)

    # Handle the admin's custom coin amount input before normal token handling.
    if user.id == ADMIN_CHAT_ID and context.user_data.get("awaiting_custom_coins"):
        if text == "/cancel":
            context.user_data.pop("awaiting_custom_coins", None)
            await update.message.reply_text("❌ Custom coin operation cancelled.", reply_markup=menu_keyboard(user.id))
            return

        pending = context.user_data.pop("awaiting_custom_coins")
        try:
            amount = int(text)
        except ValueError:
            context.user_data["awaiting_custom_coins"] = pending
            await update.message.reply_text("❌ Invalid amount. Send a whole positive number, e.g. <code>250</code>.", parse_mode="HTML")
            return

        if amount <= 0 or amount > 1_000_000:
            context.user_data["awaiting_custom_coins"] = pending
            await update.message.reply_text("❌ Amount must be between 1 and 1,000,000 coins.")
            return

        user_id = pending["user_id"]
        mode = pending["mode"]
        if is_admin_user(user_id):
            await update.message.reply_text("👑 Admin has unlimited coins.", reply_markup=menu_keyboard(user.id))
            return

        if mode == "add":
            if not add_coins(user_id, amount):
                await update.message.reply_text("❌ User wallet not found.", reply_markup=menu_keyboard(user.id))
                return
            title = "COINS ADDED"
            detail = f"Added: <b>{amount} COINS</b>"
        else:
            if not remove_coins(user_id, amount):
                await update.message.reply_text("❌ Insufficient balance or user not found.", reply_markup=menu_keyboard(user.id))
                return
            title = "COINS REMOVED"
            detail = f"Removed: <b>{amount} COINS</b>"

        balance = get_balance(user_id)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🪙 <b>{title}</b>\n\n{detail}\n💰 New Balance: <b>{balance} COINS</b>",
                parse_mode="HTML",
                reply_markup=fixed_keyboard(),
            )
        except Exception:
            logger.exception("Could not notify user about custom coin change.")

        await update.message.reply_text(
            f"🪙 <b>{title}</b>\n\n👤 User ID: <code>{user_id}</code>\n{detail}\n💰 New Balance: <b>{balance} COINS</b>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(user.id),
        )
        return

    if user.id == ADMIN_CHAT_ID and context.user_data.pop("awaiting_broadcast", False):
        if text == "/cancel":
            await update.message.reply_text("❌ Broadcast cancelled.", reply_markup=menu_keyboard(user.id))
            return
        users = get_all_users()
        sent = 0
        failed = 0
        for row in users:
            target_id = row[0]
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text=(
                        "📢 <b>ADMIN ANNOUNCEMENT</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"{escape(text)}\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode="HTML",
                    reply_markup=fixed_keyboard(False),
                )
                sent += 1
            except Exception:
                failed += 1
                logger.exception("Broadcast failed for user %s", target_id)
        await update.message.reply_text(
            "📢 <b>BROADCAST COMPLETE</b>\n\n"
            f"✅ Sent: <b>{sent}</b>\n"
            f"❌ Failed: <b>{failed}</b>\n"
            f"👥 Total targeted: <b>{len(users)}</b>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(user.id),
        )
        return

    if not is_access_token(text):
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ❌ <b>INVALID ACCESS TOKEN</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "⚠️ <b>Token Verification Failed</b>\n\n"
            "📏 Minimum Length: <b>50 Characters</b>\n"
            "🔤 Allowed: <b>a-z + 0-9</b>\n"
            "🚫 Spaces / Symbols: <b>Not Allowed</b>\n\n"
            "🔄 <i>Please check your Access Token and try again.</i>",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )
        return

    # Atomic balance check + deduction so the same coin cannot be spent twice.
    if not spend_coins(user.id, SECURITY_CODE_COST):
        balance = get_balance(user.id)
        await update.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   🪙 <b>INSUFFICIENT COINS</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"💰 Your Balance: <b>{display_balance(user.id)} COINS</b>\n"
            f"🔐 Required: <b>{SECURITY_CODE_COST} COIN</b>\n\n"
            "👉 Use <b>💰 BUY COINS</b> to request coins from admin.",
            parse_mode="HTML",
            reply_markup=menu_keyboard(update.effective_user.id),
        )
        return

    request_id = create_request(user, text)
    now = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    # UI-only Terminal Hacker animation; existing verification/coin logic is unchanged.
    scan_frames = [
        "⚡ <b>TERMINAL SECURITY SCAN</b>\n\n▰▱▱▱▱▱▱▱▱▱ 10%\n\n🔍 <b>CHECKING TOKEN...</b>\n└─ Analyzing token structure...",
        "⚡ <b>TERMINAL SECURITY SCAN</b>\n\n▰▰▰▱▱▱▱▱▱▱ 30%\n\n🔍 <b>CHECKING TOKEN...</b> ✅\n🛡️ <b>VERIFYING AUTHENTICATION...</b>\n└─ Checking token validity...",
        "⚡ <b>TERMINAL SECURITY SCAN</b>\n\n▰▰▰▰▰▱▱▱▱▱ 50%\n\n🟢 <b>TOKEN VERIFIED</b>\n⚠️ <b>CHECKING SYSTEM VULNERABILITIES...</b>\n└─ Running security integrity checks...",
        "⚡ <b>TERMINAL SECURITY SCAN</b>\n\n▰▰▰▰▰▰▰▱▱▱ 70%\n\n⚠️ <b>CHECKING SYSTEM VULNERABILITIES...</b> ✅\n⚡ <b>RUNNING FINAL SECURITY SCAN...</b>\n└─ Searching security layers...",
        "⚡ <b>TERMINAL SECURITY SCAN</b>\n\n▰▰▰▰▰▰▰▰▰▱ 90%\n\n🔐 <b>SEARCHING SECURITY CODE...</b>\n└─ Finalizing verification...",
        "💀 <b>SECURITY SCAN COMPLETE</b>\n\n━━━━━━━━━━━━━━━━━━━━━━\n✅ <b>SECURITY CODE FOUND!</b>\n━━━━━━━━━━━━━━━━━━━━━━",
    ]
    scan_message = None
    try:
        scan_message = await update.message.reply_text(scan_frames[0], parse_mode="HTML")
        for frame in scan_frames[1:]:
            await asyncio.sleep(0.85)
            try:
                await scan_message.edit_text(frame, parse_mode="HTML")
            except Exception:
                # Animation is cosmetic. If Telegram refuses an edit, do not
                # interrupt the existing verification/security-code flow.
                logger.exception("Could not update token scan animation.")
                break
    except Exception:
        logger.exception("Could not start token scan animation.")

    admin_text = (
        "📩 <b>NEW TOKEN REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {escape(user.full_name)}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🧪 <b>Access Token:</b>\n"
        f"<code>{escape(text)}</code>\n\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        f"🪙 Coin Charged: <b>{0 if is_admin_user(user.id) else SECURITY_CODE_COST}</b>\n"
        f"🕐 Time: <code>{escape(now)}</code>\n"
        "📊 Status: <b>PENDING</b>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_text,
            parse_mode="HTML",
            reply_markup=admin_panel(request_id),
        )
    except Exception:
        logger.exception("Could not notify admin.")

    code = random_security_code()
    vid = verification_id()
    balance = get_balance(user.id)

    await update.message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✨ <b>VERIFICATION COMPLETE</b>   ✨\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔐 <b>SECURITY CODE FOUND!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🧾 Verification ID: <code>{escape(vid)}</code>\n"
        "🔑 <b>YOUR SECURITY CODE</b>\n\n"
        "╔══════════════════════╗\n"
        f"║      <code>{code}</code>      ║\n"
        "╚══════════════════════╝\n\n"
        "🟢 <b>Verification: Successful</b>\n"
        "🛡️ <b>Token Format: VALID</b>\n"
        f"🪙 <b>Coins Used:</b> {SECURITY_CODE_COST}\n"
        f"💰 <b>Remaining:</b> {balance}\n\n"
        "╭──────────────────────╮\n"
        "   💎 <b>OFFICIAL VERIFICATION</b>\n"
        "╰──────────────────────╯\n\n"
        f"🤖 <b>Developer</b> • {escape(BOT_DEVELOPER)}\n"
        f"👑 <b>Owner</b> • {escape(BOT_OWNER)}\n"
        f"📢 <b>Channel</b> • {escape(CHANNEL_1)}\n\n"
        "✨ <i>Verified • Secure • Ready</i>",
        parse_mode="HTML",
        reply_markup=menu_keyboard(update.effective_user.id),
    )


# =========================
# ADMIN PANEL
# =========================
async def admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    # Close is a user-facing button, so it must work for everyone.
    if q.data == "close:0":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass
        return

    if q.from_user.id != ADMIN_CHAT_ID:
        await q.answer("⛔ Admin only.", show_alert=True)
        return

    await q.answer()

    action, request_id_text = q.data.split(":", 1)

    # Some callbacks contain two values, e.g. removecoins:<user_id>:<amount>.
    # Do not convert the whole payload to int before handling those callbacks.
    request_id = None
    if action not in ("removecoins", "adminaddamount", "adminremoveamount", "admincustom"):
        try:
            request_id = int(request_id_text)
        except ValueError:
            await q.answer("Invalid request.", show_alert=True)
            return

    # Broadcast mode: ask the admin for the next text message.
    if action == "adminbroadcast":
        context.user_data["awaiting_broadcast"] = True
        await q.message.reply_text(
            "📢 <b>BROADCAST MESSAGE</b>\n\n"
            "Send the message you want to broadcast to all registered users.\n"
            "⚠️ Only the next text message will be broadcast.\n\n"
            "Use /cancel to cancel.",
            parse_mode="HTML",
        )
        return

    # Custom coin amount: ask admin for any positive integer amount.
    if action == "admincustom":
        parts = request_id_text.split(":", 1)
        if len(parts) != 2:
            await q.answer("Invalid custom amount request.", show_alert=True)
            return
        try:
            user_id = int(parts[0])
        except ValueError:
            await q.answer("Invalid user ID.", show_alert=True)
            return
        mode = parts[1]
        if mode not in ("add", "remove"):
            await q.answer("Invalid coin operation.", show_alert=True)
            return
        if is_admin_user(user_id):
            await q.answer("👑 Admin has unlimited coins.", show_alert=True)
            return
        balance = get_balance(user_id)
        context.user_data["awaiting_custom_coins"] = {"user_id": user_id, "mode": mode}
        await q.message.reply_text(
            ("✏️ <b>CUSTOM ADD COINS</b>" if mode == "add" else "✏️ <b>CUSTOM REMOVE COINS</b>")
            + f"\n\n👤 User ID: <code>{user_id}</code>\n💰 Current Balance: <b>{balance} COINS</b>\n\n"
            + "Send the exact number of coins you want to " + ("add" if mode == "add" else "remove") + ".\n"
            + "Example: <code>250</code>\n\n❌ Use /cancel to cancel.",
            parse_mode="HTML",
        )
        return

    # Remove coins from a user.
    # The first button opens the amount selector for that request's user.
    if action == "coinremove":
        row = get_coin_request(request_id)
        if not row:
            await q.message.reply_text("❌ Coin request not found.")
            return

        user_id = row[1]
        balance = get_balance(user_id)

        await q.message.reply_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "   ➖ <b>REMOVE COINS</b>   ✦\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"🆔 User ID: <code>{user_id}</code>\n"
            f"💰 Current Balance: <b>{balance} COINS</b>\n\n"
            "👇 <b>Select amount to remove:</b>",
            parse_mode="HTML",
            reply_markup=remove_coins_keyboard(user_id),
        )
        return

    # Direct remove-coins amount callback.
    if action == "removecoins":
        # request_id_text is encoded as user_id:amount here.
        parts = request_id_text.split(":", 1)
        if len(parts) != 2:
            await q.answer("Invalid remove request.", show_alert=True)
            return

        try:
            user_id = int(parts[0])
            amount = int(parts[1])
        except ValueError:
            await q.answer("Invalid remove request.", show_alert=True)
            return
        if amount <= 0 or amount > 1_000_000:
            await q.answer("Invalid coin amount.", show_alert=True)
            return
        if is_admin_user(user_id):
            await q.answer("👑 Admin has unlimited coins.", show_alert=True)
            return
        balance_before = get_balance(user_id)

        if balance_before < amount:
            await q.answer(
                f"Insufficient balance. Current: {balance_before} coins.",
                show_alert=True,
            )
            return

        if not remove_coins(user_id, amount):
            await q.answer("Could not remove coins.", show_alert=True)
            return

        balance_after = get_balance(user_id)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    "   ➖ <b>COINS REMOVED</b>   ✦\n"
                    "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    f"🪙 Removed: <b>{amount} COINS</b>\n"
                    f"💰 New Balance: <b>{balance_after} COINS</b>"
                ),
                parse_mode="HTML",
                reply_markup=fixed_keyboard(),
            )
        except Exception:
            logger.exception("Could not notify user about coin removal.")

        await q.message.reply_text(
            "✅ <b>COINS REMOVED</b>\n\n"
            f"🆔 User ID: <code>{user_id}</code>\n"
            f"🪙 Removed: <b>{amount} COINS</b>\n"
            f"💰 New Balance: <b>{balance_after} COINS</b>",
            parse_mode="HTML",
        )
        return

    # Admin panel / user management
    if action == "adminpanel":
        # When opened from a coin request, jump directly to that request's
        # USER ADMIN PANEL. This makes the button immediately useful after
        # an approval/rejection instead of leaving the admin on a dead-end
        # request screen. For the generic admin panel (request_id=0), keep
        # the normal dashboard.
        if request_id and request_id > 0:
            coin_row = get_coin_request(request_id)
            if coin_row:
                user_id = coin_row[1]
                con = db()
                user_row = con.execute(
                    "SELECT user_id, name, username, coins, created_at, updated_at FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                con.close()
                if user_row:
                    _, name, username, balance, created_at, updated_at = user_row
                    total_bought, approved_count, pending_count, rejected_count = get_user_purchase_stats(user_id)
                    panel_text = (
                        "👤 <b>USER ADMIN PANEL</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"Name: <b>{escape(name)}</b>\n"
                        f"Username: {escape('@' + username if username else 'Not set')}\n"
                        f"Telegram ID: <code>{user_id}</code>\n"
                        f"💰 Current Balance: <b>{balance} COINS</b>\n\n"
                        f"🪙 Total Purchased/Approved: <b>{total_bought} COINS</b>\n"
                        f"✅ Approved Orders: <b>{approved_count}</b>\n"
                        f"⏳ Pending Orders: <b>{pending_count}</b>\n"
                        f"❌ Rejected Orders: <b>{rejected_count}</b>\n\n"
                        f"📅 Joined: <code>{escape(created_at)}</code>\n"
                        f"🔄 Last Updated: <code>{escape(updated_at)}</code>"
                    )
                    try:
                        await q.edit_message_text(
                            panel_text,
                            parse_mode="HTML",
                            reply_markup=admin_user_actions_keyboard(user_id),
                        )
                    except Exception:
                        logger.exception("Could not open user admin panel from coin request.")
                        await q.message.reply_text(
                            panel_text,
                            parse_mode="HTML",
                            reply_markup=admin_user_actions_keyboard(user_id),
                        )
                    return

        panel_text = (
            "🛠️ <b>ADMIN PANEL</b>\n\n"
            "Manage users and coin requests from the options below.\n\n"
            "👥 <b>ALL USERS & HISTORY</b> shows every registered user, current balance, total approved purchased coins and purchase history.\n"
            "➕ / ➖ From the user panel you can add or deduct coins anytime."
        )
        try:
            await q.edit_message_text(
                panel_text,
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
        except Exception:
            logger.exception("Could not open admin panel.")
            await q.message.reply_text(
                panel_text,
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
        return

    if action == "backcoin":
        row = get_coin_request(request_id)
        if not row:
            await q.answer("Coin request not found.", show_alert=True)
            return
        _, user_id, name, username, coins, price, status, created_at = row
        await q.edit_message_text(
            f"💰 <b>COIN REQUEST #{request_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: {escape(name)}\n"
            f"🪙 Requested: <b>{coins} COINS</b>\n"
            f"💵 Price: <b>{escape(price)}</b>\n"
            f"📊 Status: <b>{status}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
            reply_markup=coin_admin_panel(request_id, status),
        )
        return

    if action == "adminusers":
        users = get_all_users()
        if not users:
            await q.edit_message_text(
                "👥 <b>ALL USERS</b>\n\nNo users found.",
                parse_mode="HTML",
                reply_markup=admin_panel_keyboard(),
            )
            return
        total_users = len(users)
        await q.edit_message_text(
            "👥 <b>ALL USERS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Total Users: <b>{total_users}</b>\n\n"
            "Tap a user to see their complete info, balance, purchased coins and history.",
            parse_mode="HTML",
            reply_markup=admin_users_keyboard(users),
        )
        return

    if action == "adminuser":
        user_id = request_id
        con = db()
        user_row = con.execute(
            "SELECT user_id, name, username, coins, created_at, updated_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        con.close()
        if not user_row:
            await q.answer("User not found.", show_alert=True)
            return
        _, name, username, balance, created_at, updated_at = user_row
        total_bought, approved_count, pending_count, rejected_count = get_user_purchase_stats(user_id)
        panel_text = (
            "👤 <b>USER ADMIN PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Name: <b>{escape(name)}</b>\n"
            f"Username: {escape('@' + username if username else 'Not set')}\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"💰 Current Balance: <b>{balance} COINS</b>\n\n"
            f"🪙 Total Purchased/Approved: <b>{total_bought} COINS</b>\n"
            f"✅ Approved Orders: <b>{approved_count}</b>\n"
            f"⏳ Pending Orders: <b>{pending_count}</b>\n"
            f"❌ Rejected Orders: <b>{rejected_count}</b>\n\n"
            f"📅 Joined: <code>{escape(created_at)}</code>\n"
            f"🔄 Last Updated: <code>{escape(updated_at)}</code>"
        )

        # The ADMIN PANEL button can be pressed directly under the payment
        # screenshot. That original Telegram message is a PHOTO message, so
        # edit_message_text() cannot be used on it. Reply with the user panel
        # for photo messages; edit normal text messages in-place.
        try:
            if q.message and q.message.photo:
                await q.message.reply_text(
                    panel_text,
                    parse_mode="HTML",
                    reply_markup=admin_user_actions_keyboard(user_id),
                )
            else:
                await q.edit_message_text(
                    panel_text,
                    parse_mode="HTML",
                    reply_markup=admin_user_actions_keyboard(user_id),
                )
        except Exception:
            logger.exception("Could not open user admin panel.")
            await q.message.reply_text(
                panel_text,
                parse_mode="HTML",
                reply_markup=admin_user_actions_keyboard(user_id),
            )
        return

    if action == "adminuserinfo":
        user_id = request_id
        con = db()
        row = con.execute(
            "SELECT user_id, name, username, coins, created_at, updated_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        con.close()
        if not row:
            await q.answer("User not found.", show_alert=True)
            return
        _, name, username, balance, created_at, updated_at = row
        await q.message.reply_text(
            "👤 <b>USER INFO</b>\n\n"
            f"Name: <b>{escape(name)}</b>\n"
            f"Username: {escape('@' + username if username else 'Not set')}\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"Current Balance: <b>{balance} COINS</b>\n"
            f"Joined: <code>{escape(created_at)}</code>\n"
            f"Last Updated: <code>{escape(updated_at)}</code>",
            parse_mode="HTML",
            reply_markup=admin_user_actions_keyboard(user_id),
        )
        return

    if action == "adminhistory":
        user_id = request_id
        history = get_user_full_history(user_id)
        total_bought, approved_count, pending_count, rejected_count = get_user_purchase_stats(user_id)
        lines = [
            "🧾 <b>FULL USER HISTORY</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"👤 User ID: <code>{user_id}</code>",
            f"🪙 Total Approved Coins: <b>{total_bought}</b>",
            "🔐 Submitted Access Tokens are also shown below.",
            "",
        ]
        if not history:
            lines.append("No history found for this user.")
        else:
            for rid, kind, coins, price, status, created_at, token_value in history[:30]:
                icon = "✅" if status == "APPROVED" else "⏳" if status == "PENDING" else "❌"
                if kind == "TOKEN":
                    lines.append(
                        f"🔐 <b>ACCESS TOKEN / REQUEST #{rid}</b> • {icon} <b>{status}</b>\n"
                        f"   🔑 <code>{escape(token_value or 'N/A')}</code>\n"
                        f"   🕐 {escape(created_at)}"
                    )
                else:
                    lines.append(
                        f"💰 <b>COIN ORDER #{rid}</b> • {icon} <b>{status}</b>\n"
                        f"   🪙 {coins} coins • {escape(price or '')}\n"
                        f"   🕐 {escape(created_at)}"
                    )
        await q.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=admin_user_actions_keyboard(user_id),
        )
        return

    if action in ("adminadd", "adminremove"):
        user_id = request_id
        if is_admin_user(user_id):
            await q.answer("👑 Admin has unlimited coins.", show_alert=True)
            return
        mode = "add" if action == "adminadd" else "remove"
        balance = get_balance(user_id)
        await q.edit_message_text(
            ("➕ <b>ADD COINS</b>" if mode == "add" else "➖ <b>REMOVE COINS</b>")
            + f"\n\n👤 User ID: <code>{user_id}</code>\n💰 Current Balance: <b>{balance} COINS</b>\n\n👇 Select amount:",
            parse_mode="HTML",
            reply_markup=admin_amount_keyboard(user_id, mode),
        )
        return

    if action in ("adminaddrequest",):
        row = get_coin_request(request_id)
        if not row:
            await q.answer("Coin request not found.", show_alert=True)
            return
        user_id = row[1]
        balance = get_balance(user_id)
        await q.edit_message_text(
            "➕ <b>ADD COINS</b>\n\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"💰 Current Balance: <b>{balance} COINS</b>\n\n"
            "👇 Select amount to add:",
            parse_mode="HTML",
            reply_markup=admin_amount_keyboard(user_id, "add"),
        )
        return

    if action in ("adminaddamount", "adminremoveamount"):
        parts = request_id_text.split(":", 1)
        if len(parts) != 2:
            await q.answer("Invalid coin amount request.", show_alert=True)
            return
        user_id = int(parts[0])
        amount = int(parts[1])
        if amount <= 0 or amount > 1_000_000:
            await q.answer("Invalid coin amount.", show_alert=True)
            return
        if is_admin_user(user_id):
            await q.answer("👑 Admin has unlimited coins.", show_alert=True)
            return
        is_add = action == "adminaddamount"
        if is_add:
            if not add_coins(user_id, amount):
                await q.answer("User wallet not found.", show_alert=True)
                return
            title = "COINS ADDED"
            detail = f"Added: <b>{amount} COINS</b>"
        else:
            if not remove_coins(user_id, amount):
                await q.answer("Insufficient balance or user not found.", show_alert=True)
                return
            title = "COINS REMOVED"
            detail = f"Removed: <b>{amount} COINS</b>"
        balance = get_balance(user_id)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🪙 <b>{title}</b>\n\n{detail}\n💰 New Balance: <b>{balance} COINS</b>",
                parse_mode="HTML",
                reply_markup=fixed_keyboard(),
            )
        except Exception:
            logger.exception("Could not notify user about manual coin change.")
        await q.edit_message_text(
            f"🪙 <b>{title}</b>\n\n👤 User ID: <code>{user_id}</code>\n{detail}\n💰 New Balance: <b>{balance} COINS</b>",
            parse_mode="HTML",
            reply_markup=admin_user_actions_keyboard(user_id),
        )
        return

    # Coin purchase requests
    if action.startswith("coin"):
        row = get_coin_request(request_id)

        if not row:
            await q.message.reply_text("❌ Coin request not found.")
            return

        _, user_id, name, username, coins, price, status, created_at = row

        if action == "coinuser":
            await q.message.reply_text(
                "👤 <b>COIN REQUEST USER</b>\n\n"
                f"Name: <b>{escape(name)}</b>\n"
                f"Username: {escape('@' + username if username else 'Not set')}\n"
                f"Telegram ID: <code>{user_id}</code>\n"
                f"Request ID: <code>#{request_id}</code>\n"
                f"Coins: <b>{coins}</b>\n"
                f"Price: <b>{escape(price)}</b>\n"
                f"Created: <code>{escape(created_at)}</code>",
                parse_mode="HTML",
            )
            return

        if status != "PENDING":
            await q.answer(f"Already {status.lower()}.", show_alert=True)
            return

        if action == "coinapprove":
            if not has_payment_proof(request_id):
                await q.answer("Payment screenshot is required before approval.", show_alert=True)
                return

            if not add_coins(user_id, coins):
                await q.answer("❌ User wallet not found.", show_alert=True)
                return

            set_coin_request_status(request_id, "APPROVED")
            new_balance = get_balance(user_id)

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                        "   ✅ <b>COINS ADDED</b>   ✦\n"
                        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                        f"🪙 Added: <b>{coins} COINS</b>\n"
                        f"💰 New Balance: <b>{new_balance} COINS</b>\n\n"
                        "✨ You can now use <b>FIND SECURITY CODE</b>."
                    ),
                    parse_mode="HTML",
                    reply_markup=fixed_keyboard(),
                )
            except Exception:
                logger.exception("Could not notify user about coin approval.")

            # The admin notification is a PHOTO message, so its visible text
            # is the caption. Edit the caption instead of message text.
            try:
                await q.edit_message_caption(
                    caption=(
                        f"✅ <b>COIN REQUEST #{request_id}</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 User: {escape(name)}\n"
                        f"🪙 Added: <b>{coins} COINS</b>\n"
                        f"💰 New Balance: <b>{new_balance}</b>\n"
                        "📊 Status: <b>APPROVED</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "🛠️ <b>Open ADMIN PANEL below for user controls.</b>"
                    ),
                    parse_mode="HTML",
                    reply_markup=coin_admin_panel(request_id, "APPROVED"),
                )
            except Exception:
                logger.exception("Could not update payment screenshot message after approval.")
                try:
                    await q.edit_message_reply_markup(
                        reply_markup=coin_admin_panel(request_id, "APPROVED")
                    )
                except Exception:
                    logger.exception("Could not remove payment action buttons after approval.")
            return

        if action == "coinreject":
            set_coin_request_status(request_id, "REJECTED")

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ <b>COIN REQUEST REJECTED</b>\n\n"
                        "Your coin purchase request was rejected by admin."
                    ),
                    parse_mode="HTML",
                    reply_markup=fixed_keyboard(),
                )
            except Exception:
                logger.exception("Could not notify user about coin rejection.")

            # This notification contains a photo, so update its caption.
            try:
                await q.edit_message_caption(
                    caption=(
                        f"❌ <b>COIN REQUEST #{request_id}</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"👤 User: {escape(name)}\n"
                        f"🪙 Requested: <b>{coins} COINS</b>\n"
                        "📊 Status: <b>REJECTED</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode="HTML",
                    reply_markup=coin_admin_panel(request_id, "REJECTED"),
                )
            except Exception:
                logger.exception("Could not update payment screenshot message after rejection.")
                try:
                    await q.edit_message_reply_markup(
                        reply_markup=coin_admin_panel(request_id, "REJECTED")
                    )
                except Exception:
                    logger.exception("Could not remove payment action buttons after rejection.")
            return

    # Existing security-code request actions.
    row = get_request(request_id)

    if not row:
        await q.message.reply_text("❌ Request not found.")
        return

    _, user_id, name, username, token_value, status, created_at = row

    if action == "user":
        await q.message.reply_text(
            "👤 <b>USER INFORMATION</b>\n\n"
            f"Name: <b>{escape(name)}</b>\n"
            f"Username: {escape('@' + username if username else 'Not set')}\n"
            f"Telegram ID: <code>{user_id}</code>\n"
            f"Request ID: <code>#{request_id}</code>\n"
            f"Created: <code>{escape(created_at)}</code>",
            parse_mode="HTML",
        )
        return

    if action == "refresh":
        c = counts()
        cc = coin_counts()
        await q.message.reply_text(
            "📊 <b>ADMIN DASHBOARD</b>\n\n"
            f"⏳ Token Pending: <b>{c['PENDING']}</b>\n"
            f"✅ Token Approved: <b>{c['APPROVED']}</b>\n"
            f"❌ Token Rejected: <b>{c['REJECTED']}</b>\n\n"
            f"🪙 Coin Pending: <b>{cc['PENDING']}</b>\n"
            f"✅ Coin Approved: <b>{cc['APPROVED']}</b>\n"
            f"❌ Coin Rejected: <b>{cc['REJECTED']}</b>",
            parse_mode="HTML",
        )
        return

    if status != "PENDING":
        await q.answer(f"Already {status.lower()}.", show_alert=True)
        return

    if action == "approve":
        set_status(request_id, "APPROVED")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ <b>USER VERIFICATION APPROVED</b>\n\n"
                    "Your verification request has been approved by admin."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not notify user.")

        await q.edit_message_text(
            f"✅ <b>REQUEST #{request_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: {escape(name)}\n"
            "📊 Status: <b>APPROVED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
        )

    elif action == "reject":
        set_status(request_id, "REJECTED")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "❌ <b>USER VERIFICATION REJECTED</b>\n\n"
                    "Your verification request was rejected."
                ),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Could not notify user.")

        await q.edit_message_text(
            f"❌ <b>REQUEST #{request_id}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: {escape(name)}\n"
            "📊 Status: <b>REJECTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML",
        )


async def coin_package_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if q.from_user.id == ADMIN_CHAT_ID:
        await q.answer("Admin account cannot purchase coins here.", show_alert=True)
        return

    await q.answer()
    _, package_id = q.data.split(":", 1)

    package = COIN_PACKAGES.get(package_id)
    if not package:
        await q.answer("Invalid package.", show_alert=True)
        return

    coins, price = package
    user = q.from_user
    request_id = create_coin_request(user, coins, price)
    qr_url = payment_qr_url(price)

    admin_text = (
        "💰 <b>NEW COIN PURCHASE REQUEST</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {escape(user.full_name)}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🪙 Requested: <b>{coins} COINS</b>\n"
        f"💵 Price: <b>{escape(price)}</b>\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        f"🕐 Time: <code>{datetime.now().strftime('%d-%m-%Y %H:%M:%S')}</code>\n"
        "📊 Status: <b>WAITING FOR PAYMENT SCREENSHOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not notify admin about coin request.")

    payment_text = (
        "💳 <b>COIN PAYMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🪙 Package: <b>{coins} COINS</b>\n"
        f"💵 Amount: <b>{escape(price)}</b>\n\n"
        f"📱 <b>UPI ID:</b> <code>{UPI_ID}</code>\n\n"
        "📲 <b>Scan the QR above and complete the payment.</b>\n"
        "The QR is generated from the UPI payment URL, so no QR image is stored in Termux.\n\n"
        "⚠️ Payment karne ke baad <b>SEND PAYMENT SCREENSHOT</b> par click karke\n"
        "successful payment ka clear screenshot isi bot mein bhejo.\n\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 PAY TO ADMIN", url=f"https://t.me/{BOT_OWNER.lstrip('@')}")],
            [InlineKeyboardButton("📸 SEND PAYMENT SCREENSHOT", callback_data=f"payscreenshot:{request_id}")],
            [InlineKeyboardButton("🏠 MAIN MENU", callback_data="close:0")],
        ]
    )

    try:
        await q.message.reply_photo(
            photo=qr_url,
            caption=payment_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception("Could not send hosted payment QR image.")
        await q.message.reply_text(
            "⚠️ <b>QR image could not be loaded.</b>\n\n" + payment_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def payment_screenshot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    try:
        request_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.answer("Invalid payment request.", show_alert=True)
        return

    row = get_coin_request(request_id)
    if not row:
        await q.answer("Payment request not found.", show_alert=True)
        return

    _, user_id, name, username, coins, price, status, created_at = row

    if q.from_user.id != user_id:
        await q.answer("This payment request belongs to another user.", show_alert=True)
        return

    if status != "PENDING":
        await q.answer(f"This request is already {status.lower()}.", show_alert=True)
        return

    await q.answer()
    context.user_data["awaiting_payment_screenshot"] = request_id
    await q.message.reply_text(
        "📸 <b>SEND PAYMENT SCREENSHOT</b>\n\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        f"🪙 Coins: <b>{coins}</b>\n"
        f"💵 Amount: <b>{escape(price)}</b>\n\n"
        "👉 Ab successful payment ka <b>screenshot as a photo</b> bhejo.\n"
        "⚠️ Screenshot clear hona chahiye.\n\n"
        "❌ Cancel karne ke liye /cancel bhejo.",
        parse_mode="HTML",
    )


async def payment_screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request_id = context.user_data.get("awaiting_payment_screenshot")
    if not request_id or not update.message or not update.message.photo:
        return

    user = update.effective_user
    row = get_coin_request(request_id)
    if not row:
        context.user_data.pop("awaiting_payment_screenshot", None)
        await update.message.reply_text("❌ Payment request not found.")
        return

    _, user_id, name, username, coins, price, status, created_at = row
    if user.id != user_id:
        return

    if status != "PENDING":
        context.user_data.pop("awaiting_payment_screenshot", None)
        await update.message.reply_text(f"❌ This request is already {status.lower()}.")
        return

    file_id = update.message.photo[-1].file_id
    save_payment_proof(request_id, file_id)

    caption = (
        "📸 <b>PAYMENT SCREENSHOT RECEIVED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {escape(name)}\n"
        f"🆔 Telegram ID: <code>{user.id}</code>\n"
        f"🪙 Coins: <b>{coins}</b>\n"
        f"💵 Amount: <b>{escape(price)}</b>\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        f"🕐 Time: <code>{datetime.now().strftime('%d-%m-%Y %H:%M:%S')}</code>\n"
        "📊 Status: <b>READY FOR ADMIN VERIFICATION</b>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=coin_admin_panel(request_id),
        )
    except Exception:
        logger.exception("Could not forward payment screenshot to admin.")
        delete_payment_proof(request_id)
        await update.message.reply_text(
            "❌ Screenshot admin ko send nahi ho saka. Please photo dobara bhejo."
        )
        return

    context.user_data.pop("awaiting_payment_screenshot", None)
    await update.message.reply_text(
        "✅ <b>PAYMENT SCREENSHOT SENT</b>\n\n"
        f"🧾 Request ID: <code>#{request_id}</code>\n"
        "⏳ Admin payment verify karega. Approval ke baad coins automatically tumhare wallet mein add ho jayenge.",
        parse_mode="HTML",
        reply_markup=menu_keyboard(user.id),
    )



# =========================
# TELEGRAM MINI APP API
# =========================
# The Mini App reads/writes the SAME SQLite database as the bot.
# Telegram initData is verified server-side; the browser never gets the bot token.
MINI_APP_ORIGIN = os.getenv("MINI_APP_ORIGIN", "https://ff-security-code-bot.vercel.app")
MINI_APP_PORT = int(os.getenv("PORT", os.getenv("MINI_APP_PORT", "8080")))
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
BOT_APP = None
BOT_LOOP = None
API_SERVER = None


def _verify_init_data(init_data):
    if not init_data:
        raise ValueError("Missing Telegram initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        raise ValueError("Missing initData hash")

    # Reject stale login data. Telegram's auth_date is seconds since epoch.
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise ValueError("Invalid auth_date")
    if auth_date <= 0 or abs(time.time() - auth_date) > 86400:
        raise ValueError("Expired initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        raise ValueError("Invalid Telegram initData")

    raw_user = pairs.get("user")
    if not raw_user:
        raise ValueError("Telegram user missing")

    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError:
        raise ValueError("Invalid Telegram user data")

    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise ValueError("Invalid Telegram user id")
    return user


def _api_user(headers):
    user = _verify_init_data(headers.get("X-Telegram-Init-Data", ""))
    return user


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _safe_history(user_id):
    # Do not expose stored access-token values to the Mini App.
    return [
        {
            "id": row[0],
            "kind": row[1],
            "coins": row[2],
            "price": row[3],
            "status": row[4],
            "created_at": row[5],
        }
        for row in get_user_full_history(user_id)
    ]


def _notify_admin(coro):
    if BOT_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)
    except Exception:
        logger.exception("Could not schedule Mini App admin notification.")


class MiniAppHandler(BaseHTTPRequestHandler):
    server_version = "FFSecurityMiniApp/1.0"

    def log_message(self, fmt, *args):
        logger.info("MiniApp API: " + fmt, *args)

    def _headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        allowed = MINI_APP_ORIGIN if MINI_APP_ORIGIN else origin
        if origin and (origin == allowed or allowed == "*"):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif not origin:
            self.send_header("Access-Control-Allow-Origin", allowed or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _reply(self, payload, status=200):
        body = _json_bytes(payload)
        self._headers(status)
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("Invalid JSON")

    def do_OPTIONS(self):
        self._headers(204)

    def do_GET(self):
        try:
            user = _api_user(self.headers)
            user_id = user["id"]
            ensure_user(type("TelegramUser", (), {
                "id": user_id,
                "full_name": ((user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Telegram User"),
                "username": user.get("username", "")
            })())

            if self.path == "/api/health":
                return self._reply({"ok": True})

            if self.path == "/api/me":
                return self._reply({
                    "ok": True,
                    "telegram_id": user_id,
                    "name": ((user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Telegram User"),
                    "username": user.get("username", ""),
                    "balance": "∞" if is_admin_user(user_id) else get_balance(user_id),
                    "status": "ADMIN" if is_admin_user(user_id) else "ACTIVE",
                    "security_code_cost": SECURITY_CODE_COST,
                    "bot_username": BOT_USERNAME,
                })

            if self.path == "/api/history":
                return self._reply({"ok": True, "items": _safe_history(user_id)})

            if self.path == "/api/packages":
                return self._reply({
                    "ok": True,
                    "packages": [
                        {"id": pid, "coins": coins, "price": price}
                        for pid, (coins, price) in COIN_PACKAGES.items()
                    ]
                })

            return self._reply({"error": "Not found"}, 404)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, 401)
        except Exception:
            logger.exception("Mini App GET failed")
            return self._reply({"error": "Server error"}, 500)

    def do_POST(self):
        try:
            user = _api_user(self.headers)
            user_id = user["id"]
            ensure_user(type("TelegramUser", (), {
                "id": user_id,
                "full_name": ((user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Telegram User"),
                "username": user.get("username", "")
            })())
            data = self._body()

            if self.path == "/api/coins/request":
                if is_admin_user(user_id):
                    return self._reply({"error": "Admin wallet is unlimited."}, 400)

                package_id = str(data.get("package_id", ""))
                package = COIN_PACKAGES.get(package_id)
                if not package:
                    return self._reply({"error": "Invalid coin package."}, 400)

                coins, price = package
                telegram_user = type("TelegramUser", (), {
                    "id": user_id,
                    "full_name": ((user.get("first_name", "") + " " + user.get("last_name", "")).strip() or "Telegram User"),
                    "username": user.get("username", "")
                })()
                request_id = create_coin_request(telegram_user, coins, price)
                qr_url = payment_qr_url(price)

                admin_text = (
                    "💰 <b>NEW COIN PURCHASE REQUEST</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 User: {escape(telegram_user.full_name)}\n"
                    f"🆔 Telegram ID: <code>{user_id}</code>\n"
                    f"🪙 Requested: <b>{coins} COINS</b>\n"
                    f"💵 Price: <b>{escape(price)}</b>\n"
                    f"🧾 Request ID: <code>#{request_id}</code>\n"
                    "📊 Status: <b>WAITING FOR PAYMENT SCREENSHOT</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                )
                if BOT_APP is not None:
                    _notify_admin(BOT_APP.bot.send_message(
                        chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode="HTML"
                    ))

                return self._reply({
                    "ok": True,
                    "request_id": request_id,
                    "coins": coins,
                    "price": price,
                    "upi_id": UPI_ID,
                    "upi_name": UPI_NAME,
                    "qr_url": qr_url,
                    "message": "Request created. Complete payment and send the screenshot in the bot for admin verification."
                })

            # Token values are deliberately not accepted by the web API.
            # The existing bot flow remains the place where that operation is handled.
            if self.path == "/api/security/scan":
                return self._reply({
                    "error": "Security scan is available through the Telegram bot flow. The Mini App does not collect or transmit access tokens."
                }, 403)

            return self._reply({"error": "Not found"}, 404)
        except ValueError as exc:
            return self._reply({"error": str(exc)}, 400)
        except Exception:
            logger.exception("Mini App POST failed")
            return self._reply({"error": "Server error"}, 500)


def start_mini_app_api():
    global API_SERVER
    try:
        API_SERVER = ThreadingHTTPServer(("0.0.0.0", MINI_APP_PORT), MiniAppHandler)
        logger.info("Mini App API listening on 0.0.0.0:%s", MINI_APP_PORT)
        API_SERVER.serve_forever()
    except Exception:
        logger.exception("Mini App API stopped.")


async def mini_app_post_init(app):
    global BOT_APP, BOT_LOOP
    BOT_APP = app
    BOT_LOOP = asyncio.get_running_loop()
    threading.Thread(target=start_mini_app_api, name="mini-app-api", daemon=True).start()

# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in the Wispbyte environment variables.")

    init_db()

    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(mini_app_post_init)
           .build())

    app.add_handler(CommandHandler("start", start))

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id == ADMIN_CHAT_ID:
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("awaiting_custom_coins", None)
            await update.message.reply_text("❌ Cancelled.", reply_markup=menu_keyboard(update.effective_user.id))
        elif context.user_data.pop("awaiting_payment_screenshot", None):
            await update.message.reply_text("❌ Payment screenshot submission cancelled.", reply_markup=menu_keyboard(update.effective_user.id))
        else:
            await update.message.reply_text("Nothing to cancel.", reply_markup=menu_keyboard(update.effective_user.id))

    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(
        CallbackQueryHandler(
            coin_package_callback,
            pattern=r"^buycoins:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            payment_screenshot_callback,
            pattern=r"^payscreenshot:",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_actions,
            pattern=r"^(approve|reject|user|refresh|coinapprove|coinreject|coinuser|coinremove|removecoins|adminpanel|adminbroadcast|backcoin|adminusers|adminuser|adminuserinfo|adminhistory|adminadd|adminremove|adminaddrequest|adminaddamount|adminremoveamount|admincustom|close):",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.Regex(
                r"^(🔎 FIND SECURITY CODE|🪙 MY COINS|💰 BUY COINS|🧾 VERIFICATION INFO|📢 CHANNELS|ℹ️ ABOUT|🛠️ SUPPORT|🏠 MAIN MENU|🛠️ ADMIN PANEL)$"
            ),
            keyboard_handler,
        )
    )

    app.add_handler(
        MessageHandler(filters.PHOTO, payment_screenshot_handler)
    )

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive)
    )

    print(f"🤖 Security Bot is running... | Mini App API port: {MINI_APP_PORT}")
    app.run_polling()


if __name__ == "__main__":
    main()
