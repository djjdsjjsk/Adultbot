import os
import time
import sqlite3
import threading
import logging
import requests
import telebot

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from requests.adapters import HTTPAdapter

# ============ CONFIGURATION ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "https://osint-s7db.onrender.com").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

# ONLY ALLOWED GROUPS
ALLOWED_GROUP_IDS = [
    -1001612436840,  # GROUP 1 ID
    -1002862248348,  # GROUP 2 ID
]

# ADMIN LIST
ADMIN_IDS = [5989928539, 1087968824]

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
                content_hash TEXT PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS protected_users (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS whitelisted_channels (
                channel_id INTEGER PRIMARY KEY,
                channel_title TEXT
            )
        """)
        conn.commit()
        logger.info("Database initialized successfully: %s", DB_PATH)
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


def is_message_logged(content_hash):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM logged_messages WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def log_message_hash(content_hash):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO logged_messages (content_hash) VALUES (?)",
            (content_hash,)
        )
        conn.commit()
    finally:
        conn.close()


def unlock_message_hash(content_hash):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM logged_messages WHERE content_hash = ?", (content_hash,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def is_user_protected(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM protected_users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def set_user_protection(user_id, protect=True):
    conn = get_db()
    try:
        if protect:
            conn.execute("INSERT OR IGNORE INTO protected_users (user_id) VALUES (?)", (user_id,))
        else:
            conn.execute("DELETE FROM protected_users WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def is_channel_whitelisted(channel_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM whitelisted_channels WHERE channel_id = ?", (channel_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def add_whitelisted_channel(channel_id, title="Channel"):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO whitelisted_channels (channel_id, channel_title)
            VALUES (?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET channel_title = excluded.channel_title
        """, (channel_id, title))
        conn.commit()
    finally:
        conn.close()


def remove_whitelisted_channel(channel_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM whitelisted_channels WHERE channel_id = ?", (channel_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def is_group_admin(chat_id, user_id):
    if user_id in ADMIN_IDS:
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return False


# ============ HEALTH-CHECK WEB SERVER ============
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
        logger.exception("Health server failed to start")


def start_self_ping():
    session = requests.Session()
    try:
        from urllib3.util.retry import Retry
        retry = Retry(
            total=2, connect=2, read=2, backoff_factor=1,
            status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"]
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass

    def worker():
        time.sleep(20)
        while True:
            try:
                response = session.get(APP_URL + "/health", timeout=15)
                response.close()
            except requests.RequestException:
                pass
            time.sleep(180)

    threading.Thread(target=worker, daemon=True, name="self-ping").start()


# ============ MESSAGE FILTERING SYSTEM ============
@bot.channel_post_handler(func=lambda message: True)
@bot.message_handler(func=lambda message: message.chat.id in ALLOWED_GROUP_IDS and (not message.text or not message.text.startswith("/")))
def handle_group_filtering(message):
    try:
        chat_id = message.chat.id
        message_id = message.id
        user_id = message.from_user.id if message.from_user else None

        if chat_id not in ALLOWED_GROUP_IDS or message.chat.type == "private":
            return

        is_protected = False

        if user_id and user_id in ADMIN_IDS:
            is_protected = True
        if not is_protected and user_id and is_user_protected(user_id):
            is_protected = True
        if not is_protected and user_id:
            try:
                member = bot.get_chat_member(chat_id, user_id)
                if member.status in ["administrator", "creator"]:
                    is_protected = True
            except Exception:
                pass
        if not is_protected:
            try:
                if message.sender_chat and message.sender_chat.id == chat_id:
                    is_protected = True
            except Exception:
                pass
        if not is_protected:
            try:
                if message.is_automatic_forward and message.sender_chat:
                    linked_chat = bot.get_chat(chat_id=chat_id)
                    if linked_chat.linked_chat_id and message.sender_chat.id == linked_chat.linked_chat_id:
                        is_protected = True
            except Exception:
                pass
        if not is_protected:
            try:
                if message.forward_from_chat and is_channel_whitelisted(message.forward_from_chat.id):
                    is_protected = True
                elif message.sender_chat and is_channel_whitelisted(message.sender_chat.id):
                    is_protected = True
            except Exception:
                pass

        if not is_protected:
            text = (message.text or message.caption or "").lower()

            is_media_file = False
            try:
                if message.animation or message.document or message.sticker:
                    is_media_file = True
            except Exception:
                pass

            has_inline_keyboard = False
            try:
                if message.reply_markup and hasattr(message.reply_markup, "inline_keyboard"):
                    if message.reply_markup.inline_keyboard:
                        has_inline_keyboard = True
            except Exception:
                pass

            is_forwarded = False
            try:
                if message.forward_from or message.forward_from_chat or message.forward_from_message_id or message.is_automatic_forward:
                    is_forwarded = True
                if hasattr(message, "forward_origin") and message.forward_origin:
                    is_forwarded = True
            except Exception:
                pass

            is_story = False
            try:
                if getattr(message, "story", None) or getattr(message, "media_story", None):
                    is_story = True
                if hasattr(message, "forward_origin") and message.forward_origin:
                    if getattr(message.forward_origin, "type", None) == "story":
                        is_story = True
            except Exception:
                pass

            is_text_quoted = False
            try:
                entities = message.entities or message.caption_entities or []
                for entity in entities:
                    if entity.type in ["blockquote", "expanded_blockquote"]:
                        is_text_quoted = True
                        break
            except Exception:
                pass

            content_hash = f"{chat_id}_{text.strip()}"
            is_blacklisted = is_message_logged(content_hash)

            if (
                is_media_file
                or has_inline_keyboard
                or is_forwarded
                or is_story
                or is_text_quoted
                or is_blacklisted
                or "http://" in text
                or "https://" in text
                or "@" in text
                or "join" in text
                or "🔞" in text
                or "💋" in text
                or "maderchod" in text
                or "👇" in text
                or "👈" in text
                or "18+" in text
                or "_bot" in text
                or "http://t.me/" in text
                or "forwarded" in text
                or "sexy" in text
                or "bot" in text
                or "bot?start=" in text
                or "t.me" in text
                or "sex" in text
                or "🥵" in text
                or "18" in text
                or "#" in text
                or "mms" in text
                or "porn" in text
                or "teen" in text
                or "www." in text
                or "cam" in text
            ):
                try:
                    bot.delete_message(chat_id=chat_id, message_id=message.id)
                except Exception:
                    pass

    except Exception:
        logger.exception("Error in group filter handler")


# ============ ADMIN COMMANDS (LOG / UNLOCK / PROTECT / CHANNEL) ============

@bot.message_handler(commands=["log"])
def log_custom_message(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        if not message.reply_to_message:
            sent = bot.reply_to(message, "⚠️ Kisi message par reply karke `/log` bhejein.")
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
            return

        replied_msg = message.reply_to_message
        if replied_msg.text or replied_msg.caption:
            content_text = (replied_msg.text or replied_msg.caption).lower().strip()
            content_hash = f"{replied_msg.chat.id}_{content_text}"
            log_message_hash(content_hash)
            
            # Delete the targeted message that is now locked
            try:
                bot.delete_message(chat_id=replied_msg.chat.id, message_id=replied_msg.id)
            except Exception:
                pass

            sent = bot.reply_to(message, "🔒 **Post Locked & Deleted!** Yeh message blacklist ho gaya hai aur delete kar diya gaya hai.")
            # Auto delete command message and bot reply after 5 seconds
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
        else:
            sent = bot.reply_to(message, "⚠️ Sirf text messages ya captions ko hi lock kiya ja sakta hai.")
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
    except Exception:
        logger.exception("Log command failed")


@bot.message_handler(commands=["unlock"])
def unlock_custom_message(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        if not message.reply_to_message:
            sent = bot.reply_to(message, "⚠️ Kisi locked message par reply karke `/unlock` bhejein.")
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
            return

        replied_msg = message.reply_to_message
        if replied_msg.text or replied_msg.caption:
            content_text = (replied_msg.text or replied_msg.caption).lower().strip()
            content_hash = f"{replied_msg.chat.id}_{content_text}"
            unlocked = unlock_message_hash(content_hash)
            if unlocked:
                sent = bot.reply_to(message, "✅ **Message Unlocked successfully!** Ab yeh message dobara bheja ja sakta hai.")
            else:
                sent = bot.reply_to(message, "ℹ️ Yeh message pehle se locked/blacklisted nahi tha.")
            
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
        else:
            sent = bot.reply_to(message, "⚠️ Sirf text messages ke liye valid hai.")
            threading.Thread(target=auto_delete_messages, args=(message.chat.id, [message.id, sent.id], 5)).start()
    except Exception:
        logger.exception("Unlock command failed")


def auto_delete_messages(chat_id, message_ids, delay=5):
    time.sleep(delay)
    for msg_id in message_ids:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


@bot.message_handler(commands=["protect"])
def protect_user_cmd(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        if not message.reply_to_message or not message.reply_to_message.from_user:
            bot.reply_to(message, "⚠️ Kisi user ke message par reply karke `/protect` bhejein.")
            return

        target_user = message.reply_to_message.from_user
        set_user_protection(target_user.id, protect=True)
        bot.reply_to(message, f"🛡️ User **{target_user.first_name}** ko protect kar diya gaya hai.")
    except Exception:
        logger.exception("Protect command failed")


@bot.message_handler(commands=["unprotect"])
def unprotect_user_cmd(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        if not message.reply_to_message or not message.reply_to_message.from_user:
            bot.reply_to(message, "⚠️ Kisi user के message par reply karke `/unprotect` bhejein.")
            return

        target_user = message.reply_to_message.from_user
        set_user_protection(target_user.id, protect=False)
        bot.reply_to(message, f"🔓 User **{target_user.first_name}** ki protection hata di gayi hai.")
    except Exception:
        logger.exception("Unprotect command failed")


@bot.message_handler(commands=["addchannel"])
def add_channel_cmd(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "⚠️ Sahi format use karein:\n`/addchannel -100xxxxxxxxxx`")
            return

        try:
            channel_id = int(args[1])
        except ValueError:
            bot.reply_to(message, "⚠️ Channel ID sirf numbers me honi chahiye.")
            return

        add_whitelisted_channel(channel_id, title=f"Channel {channel_id}")
        bot.reply_to(message, f"✅ Channel ID `{channel_id}` ko whitelist kar diya gaya hai!")
    except Exception:
        logger.exception("Add channel failed")


@bot.message_handler(commands=["delchannel"])
def del_channel_cmd(message):
    try:
        if message.from_user.id not in ADMIN_IDS and not is_group_admin(message.chat.id, message.from_user.id):
            return
        
        args = message.text.split()
        if len(args) < 2:
            bot.reply_to(message, "⚠️ Sahi format use karein:\n`/delchannel -100xxxxxxxxxx`")
            return

        try:
            channel_id = int(args[1])
        except ValueError:
            bot.reply_to(message, "⚠️ Channel ID valid number honi chahiye.")
            return

        removed = remove_whitelisted_channel(channel_id)
        if removed:
            bot.reply_to(message, f"🗑️ Channel ID `{channel_id}` ko whitelist se hata diya gaya hai.")
        else:
            bot.reply_to(message, "ℹ️ Yeh Channel ID whitelist me nahi mili.")
    except Exception:
        logger.exception("Del channel failed")


# ============ ADMIN BROADCAST SYSTEM ============
@bot.message_handler(commands=["broadcast"])
def broadcast_command(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    broadcast_states[message.from_user.id] = True
    bot.reply_to(message, "📢 **Broadcast Mode Activated!**\n\nAb aap jo bhi bhejenge, woh saare users ko broadcast kar diya jayega.\n\nCancel karne ke liye `/cancel` bhejein.")


@bot.message_handler(commands=["cancel"])
def cancel_broadcast(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.from_user.id in broadcast_states:
        broadcast_states.pop(message.from_user.id, None)
        bot.reply_to(message, "❌ Broadcast mode cancelled.")
    else:
        bot.reply_to(message, "⚠️ No active broadcast process.")


@bot.message_handler(func=lambda msg: msg.from_user.id in ADMIN_IDS and broadcast_states.get(msg.from_user.id, False), content_types=["text", "photo", "video", "audio", "document", "animation", "voice"])
def execute_broadcast(message):
    if message.text and message.text.startswith("/cancel"):
        return

    admin_i
