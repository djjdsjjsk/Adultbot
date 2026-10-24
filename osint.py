import os
import time
import sqlite3
import threading
import logging
import random
import re
import requests
import telebot
from telebot import types

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from requests.adapters import HTTPAdapter

# ============ CONFIGURATION ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = 5989928539
APP_URL = os.environ.get("APP_URL", "https://osint-s7db.onrender.com").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

POSITIVE_REACTIONS = ["👍", "❤️", "🔥", "👏", "🎉", "🤩", "💯"]

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
    raise RuntimeError(
        "BOT_TOKEN missing. Render Dashboard > Environment mein BOT_TOKEN set karein."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("telegram-bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)

broadcast_states = {}


# ============ DATABASE HELPERS ============
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                status TEXT DEFAULT 'active'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logged_messages (
                content_hash TEXT PRIMARY KEY,
                original_text TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocked_keywords (
                keyword TEXT PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_spam (
                user_id INTEGER PRIMARY KEY,
                last_msg_hash TEXT,
                count INTEGER DEFAULT 1,
                mute_until REAL DEFAULT 0
            )
        """)
        conn.commit()
        logger.info("Advanced Database initialized successfully: %s", DB_PATH)
    finally:
        conn.close()


def add_user(user_id, first_name):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO users (user_id, first_name, status)
            VALUES (?, ?, 'active')
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                status = 'active'
        """, (user_id, first_name))
        conn.commit()
    finally:
        conn.close()


def get_all_users():
    conn = get_db()
    try:
        rows = conn.execute("SELECT user_id FROM users WHERE status = 'active'").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def normalize_text(text):
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def is_message_logged(text):
    norm_text = normalize_text(text)
    content_hash = f"msg_{hash(norm_text)}"
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM logged_messages WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def log_message_text(text):
    norm_text = normalize_text(text)
    content_hash = f"msg_{hash(norm_text)}"
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO logged_messages (content_hash, original_text) VALUES (?, ?)",
            (content_hash, text)
        )
        conn.commit()
    finally:
        conn.close()


def remove_logged_message(text):
    norm_text = normalize_text(text)
    content_hash = f"msg_{hash(norm_text)}"
    conn = get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM logged_messages WHERE content_hash = ?", (content_hash,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def add_custom_keyword(keyword):
    norm_kw = normalize_text(keyword)
    if not norm_kw:
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO blocked_keywords (keyword) VALUES (?)",
            (norm_kw,)
        )
        conn.commit()
    finally:
        conn.close()


def remove_custom_keyword(keyword):
    norm_kw = normalize_text(keyword)
    conn = get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM blocked_keywords WHERE keyword = ?",
            (norm_kw,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_custom_keywords():
    conn = get_db()
    try:
        rows = conn.execute("SELECT keyword FROM blocked_keywords").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def check_and_update_spam(user_id, content_hash):
    conn = get_db()
    now = time.time()
    try:
        row = conn.execute(
            "SELECT last_msg_hash, count, mute_until FROM user_spam WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row:
            last_hash, count, mute_until = row
            if now < mute_until:
                return "muted"

            if last_hash == content_hash:
                count += 1
            else:
                count = 1
                last_hash = content_hash

            if count >= 4:
                mute_until = now + 900
                conn.execute(
                    "UPDATE user_spam SET last_msg_hash = ?, count = ?, mute_until = ? WHERE user_id = ?",
                    (last_hash, count, mute_until, user_id)
                )
                conn.commit()
                return "just_muted"
            else:
                conn.execute(
                    "UPDATE user_spam SET last_msg_hash = ?, count = ?, mute_until = 0 WHERE user_id = ?",
                    (last_hash, count, user_id)
                )
                conn.commit()
                return "active"
        else:
            conn.execute(
                "INSERT INTO user_spam (user_id, last_msg_hash, count, mute_until) VALUES (?, ?, 1, 0)",
                (user_id, content_hash)
            )
            conn.commit()
            return "active"
    finally:
        conn.close()


def get_user_mute_status(user_id):
    conn = get_db()
    now = time.time()
    try:
        row = conn.execute(
            "SELECT mute_until FROM user_spam WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and row[0] > now:
            return int(row[0] - now)
        return 0
    finally:
        conn.close()


def clear_user_mute(user_id):
    conn = get_db()
    try:
        cursor = conn.execute(
            "UPDATE user_spam SET mute_until = 0, count = 0 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ============ HEALTH-CHECK & SELF-PING ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = b"Bot service is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        pass

def start_health_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
        server.daemon_threads = True
        logger.info("Health server listening on port %s", PORT)
        server.serve_forever()
    except Exception:
        logger.exception("Health server failed")

def start_self_ping():
    session = requests.Session()
    try:
        from urllib3.util.retry import Retry
        retry = Retry(total=2, connect=2, read=2, backoff_factor=1, status_forcelist=[500,502,503,504], allowed_methods=["GET"])
        session.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass
    def worker():
        time.sleep(20)
        while True:
            try:
                res = session.get(APP_URL + "/health", timeout=15)
                res.close()
            except Exception:
                pass
            time.sleep(180)
    threading.Thread(target=worker, daemon=True, name="self-ping").start()


def is_privileged(chat_id, user_id):
    if user_id == ADMIN_ID:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        if member.status in ["creator", "administrator"]:
            return True
    except Exception:
        pass
    return False


# ============ 1. CHANNEL POSTS HANDLER (Auto Reaction) ============
@bot.channel_post_handler(func=lambda message: True)
def handle_channel_posts(message):
    try:
        chosen_reaction = random.choice(POSITIVE_REACTIONS)
        bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            reaction=[types.ReactionTypeEmoji(chosen_reaction)]
        )
    except Exception:
        pass


# ============ 2. GROUP & PRIVATE CHAT MESSAGES HANDLER ============
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    try:
        chat_id = message.chat.id
        message_id = message.id
        user_id = message.from_user.id if message.from_user else None
        chat_type = message.chat.type

        # Universal Auto Reaction on EVERY message (Private, Group, etc.)
        try:
            chosen_reaction = random.choice(POSITIVE_REACTIONS)
            bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[types.ReactionTypeEmoji(chosen_reaction)]
            )
        except Exception:
            pass

        # If it's a private chat / DM, let command handlers process it (don't filter text in DM)
        if chat_type == "private":
            return

        # --- GROUP & SUPERGROUP PROTECTION LOGIC ---
        
        # A. Linked Channel / Channel post bypass
        if message.sender_chat:
            return

        # B. Delete Service Messages (Join/Leave)
        if message.content_type in ["new_chat_members", "left_chat_member", "group_chat_created", "migrate_to_chat_id"]:
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            return

        # C. Check if user is admin or owner (Bypass filters)
        if user_id and is_privileged(chat_id, user_id):
            return

        # D. Check Mute Status
        if user_id:
            remaining_time = get_user_mute_status(user_id)
            if remaining_time > 0:
                try:
                    bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
                return

        # E. Advanced Text Filtering
        if message.text and not message.text.startswith("/"):
            raw_text = message.text
            norm_text = normalize_text(raw_text)

            # 1. URL / t.me / @ username check
            has_url = "http://" in norm_text or "https://" in norm_text or "www." in norm_text or "t.me/" in norm_text or "@" in norm_text
            
            # 2. Custom Keyword check from DB
            custom_keywords = get_custom_keywords()
            has_custom_keyword = any(kw in norm_text for kw in custom_keywords if kw)

            # 3. Logged / Exact Match Message Check
            is_logged_match = is_message_logged(raw_text)

            if has_url or has_custom_keyword or is_logged_match:
                try:
                    bot.delete_message(chat_id, message_id)
                    
                    if user_id and (is_logged_match or has_custom_keyword):
                        content_hash = f"hash_{hash(norm_text)}"
                        spam_status = check_and_update_spam(user_id, content_hash)
                        if spam_status == "just_muted":
                            bot.send_message(chat_id, f"⚠️ {message.from_user.first_name}, you have been muted for 15 minutes due to repeated spamming.")
                    return
                except Exception:
                    pass

    except Exception:
        logger.exception("Error in message handler")


# ============ ADMIN COMMANDS (LOG, LOCK, UNLOCK, ADD, REMOVE) ============
@bot.message_handler(commands=["log", "lock"])
def log_custom_message(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        if not message.reply_to_message:
            bot.reply_to(message, "⚠️ Reply to any message with /log (or /lock) to permanently lock and blacklist it.")
            return

        replied_msg = message.reply_to_message
        if replied_msg.text:
            text_to_lock = replied_msg.text
            log_message_text(text_to_lock)
            
            words = text_to_lock.split()
            for w in words:
                if "http://" in w or "https://" in w or "www." in w or "t.me/" in w or "@" in w:
                    add_custom_keyword(w)

            bot.reply_to(message, "🔒 **Advanced Locked!**\nThis message and its contents are now permanently blacklisted.", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ Only text messages can be locked.")
    except Exception:
        logger.exception("Log command failed")


@bot.message_handler(commands=["unlock"])
def unlock_message_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        
        text_to_unlock = ""
        if message.reply_to_message and message.reply_to_message.text:
            text_to_unlock = message.reply_to_message.text
        else:
            parts = message.text.split(maxsplit=1)
            if len(parts) < 2:
                bot.reply_to(message, "Usage:\n1. Reply to a locked message with `/unlock`\n2. Or type `/unlock <word_or_url>`", parse_mode="Markdown")
                return
            text_to_unlock = parts[1].strip()

        removed_log = remove_logged_message(text_to_unlock)
        removed_kw = remove_custom_keyword(text_to_unlock)

        if removed_log or removed_kw:
            bot.reply_to(message, f"🔓 **Successfully Unlocked!**\n`{text_to_unlock[:50]}` is now removed from the blacklist.", parse_mode="Markdown")
        else:
            bot.reply_to(message, "⚠️ This message or keyword was not found in the locked/blacklist records.")
    except Exception:
        logger.exception("Unlock command failed")


@bot.message_handler(commands=["add"])
def add_keyword_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: `/add <word_phrase_or_url>`", parse_mode="Markdown")
            return
        kw = parts[1].strip()
        add_custom_keyword(kw)
        bot.reply_to(message, f"✅ Added to advanced blocklist: `{kw}`", parse_mode="Markdown")
    except Exception:
        logger.exception("Add keyword failed")


@bot.message_handler(commands=["remove", "del"])
def remove_keyword_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(message, "Usage: `/remove <word_phrase_or_url>`", parse_mode="Markdown")
            return
        kw = parts[1].strip()
        if remove_custom_keyword(kw) or remove_logged_message(kw):
            bot.reply_to(message, f"✅ Removed from blocklist: `{kw}`", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"⚠️ Keyword not found in blocklist.")
    except Exception:
        logger.exception("Remove keyword failed")


@bot.message_handler(commands=["listwords", "list"])
def list_keywords_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        keywords = get_custom_keywords()
        if not keywords:
            bot.reply_to(message, "📋 No custom blocked words/URLs added yet.")
            return
        text = "📋 **Advanced Blocked Keywords & URLs:**\n\n" + "\n".join(f"• `{kw}`" for kw in keywords)
        bot.reply_to(message, text, parse_mode="Markdown")
    except Exception:
        logger.exception("List keywords failed")


@bot.message_handler(commands=["unmute"])
def unmute_user_command(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            bot.reply_to(message, "Usage: `/unmute <user_id>`", parse_mode="Markdown")
            return
        target_uid = int(parts[1].strip())
        if clear_user_mute(target_uid):
            bot.reply_to(message, f"✅ User `{target_uid}` has been unmuted successfully.", parse_mode="Markdown")
        else:
            bot.reply_to(message, f"⚠️ User `{target_uid}` was not found in spam/mute records.", parse_mode="Markdown")
    except Exception:
        logger.exception("Unmute command failed")


# ============ BROADCAST SYSTEM ============
@bot.message_handler(commands=["broadcast"])
def broadcast_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    broadcast_states[ADMIN_ID] = True
    bot.reply_to(message, "📢 **Broadcast Mode Activated!**\n\nAb jo bhi bhejenge (Media, Video, Text, Link ke sath), woh sabhi users ko broadcast ho jayega.\nCancel karne ke liye `/cancel` bhejein.")


@bot.message_handler(commands=["cancel"])
def cancel_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    if ADMIN_ID in broadcast_states:
        del broadcast_states[ADMIN_ID]
        bot.reply_to(message, "❌ Broadcast cancelled.")
    else:
        bot.reply_to(message, "⚠️ No active broadcast process.")


@bot.message_handler(func=lambda msg: msg.from_user.id == ADMIN_ID and broadcast_states.get(ADMIN_ID, False), content_types=["text", "photo", "video", "audio", "document", "animation", "voice"])
def execute_broadcast(message):
    if message.text and message.text.startswith("/cancel"):
        return

    broadcast_states.pop(ADMIN_ID, None)
    users = get_all_users()
    success = 0
    failed = 0

    status_msg = bot.reply_to(message, f"🚀 Broadcasting to {len(users)} users...")

    for uid in users:
        try:
            bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.id)
            success += 1
            time.sleep(0.05)
        except Exception:
            failed += 1

    try:
        bot.edit_message_text(
            f"✅ **Broadcast Finished!**\n\n• Success: {success}\n• Failed: {failed}",
            chat_id=message.chat.id,
            message_id=status_msg.id,
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ============ START COMMAND WITH REPLY KEYBOARD ============
@bot.message_handler(commands=["start"])
def send_welcome(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name or "User"
        add_user(user_id, first_name)

        if user_id == ADMIN_ID:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
            broadcast_btn = types.KeyboardButton("📢 Broadcast")
            markup.add(broadcast_btn)

            text = (
                "👑 **Advanced Admin Control Panel**\n\n"
                "• **Broadcast:** Niche diye gaye button par click karein.\n"
                "• **Add Word/URL:** `/add <word_or_url>`\n"
                "• **Remove Word:** `/remove <word_or_url>`\n"
                "• **View Blocklist:** `/listwords`\n"
                "• **Lock Message:** Reply messa
