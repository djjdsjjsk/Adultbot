import os
import time
import sqlite3
import threading
import logging
import random
import requests
import telebot

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

# Temporary memory to handle multi-step broadcast for admin
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
            CREATE TABLE IF NOT EXISTS user_spam (
                user_id INTEGER PRIMARY KEY,
                last_msg_hash TEXT,
                count INTEGER DEFAULT 1,
                mute_until REAL DEFAULT 0
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


# ============ SELF-PING EVERY 3 MINUTES ============
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
                logger.info("Self-ping: HTTP %s", response.status_code)
                response.close()
            except requests.RequestException:
                logger.exception("Self-ping failed")
            time.sleep(180)

    threading.Thread(target=worker, daemon=True, name="self-ping").start()


# ============ AUTO REACTIONS & MESSAGE FILTERING ============
@bot.channel_post_handler(func=lambda message: True)
@bot.message_handler(func=lambda message: message.chat.type in ["group", "supergroup"] and (not message.text or not message.text.startswith("/")))
def handle_reactions_and_filtering(message):
    try:
        chat_id = message.chat.id
        message_id = message.id
        user_id = message.from_user.id if message.from_user else None

        # 1. Auto Positive Reaction
        try:
            chosen_reaction = random.choice(POSITIVE_REACTIONS)
            bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=[telebot.types.ReactionTypeEmoji(chosen_reaction)])
        except Exception:
            pass

        # 2. Delete Service Messages (Join/Leave)
        if message.content_type in ["new_chat_members", "left_chat_member", "group_chat_created", "migrate_to_chat_id"]:
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass
            return

        # 3. Check Mute Status
        if user_id and user_id != ADMIN_ID:
            remaining_time = get_user_mute_status(user_id)
            if remaining_time > 0:
                try:
                    bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
                return

        # 4. URL, t.me & Username Filter
        if message.text:
            text_lower = message.text.lower()
            has_url = "http://" in text_lower or "https://" in text_lower or "www." in text_lower or "t.me/" in text_lower or ("@" in text_lower and not message.text.startswith("/"))
            
            if has_url and user_id != ADMIN_ID:
                try:
                    bot.delete_message(chat_id, message_id)
                    return
                except Exception:
                    pass

            # 5. Logged / Duplicate Message Filter
            content_hash = f"{chat_id}_{text_lower.strip()}"
            if is_message_logged(content_hash) and user_id != ADMIN_ID:
                spam_status = check_and_update_spam(user_id, content_hash)
                try:
                    bot.delete_message(chat_id, message_id)
                    if spam_status == "just_muted":
                        bot.send_message(chat_id, f"⚠️ {message.from_user.first_name}, you have been muted for 15 minutes due to spamming.")
                except Exception:
                    pass
                return

    except Exception:
        logger.exception("Error in filter handler")


# ============ MANUAL MESSAGE LOGGING (/log) ============
@bot.message_handler(commands=["log"])
def log_custom_message(message):
    try:
        if message.from_user.id != ADMIN_ID:
            return
        if not message.reply_to_message:
            bot.reply_to(message, "⚠️ Reply to any message with /log to blacklist it.")
            return

        replied_msg = message.reply_to_message
        if replied_msg.text:
            content_hash = f"{replied_msg.chat.id}_{replied_msg.text.lower().strip()}"
            log_message_hash(content_hash)
            bot.reply_to(message, "✅ Message logged successfully! Future instances will be auto-deleted.")
        else:
            bot.reply_to(message, "⚠️ Only text messages can be logged.")
    except Exception:
        logger.exception("Log command failed")


# ============ ADMIN BROADCAST SYSTEM ============
@bot.message_handler(commands=["broadcast"])
def broadcast_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    
    broadcast_states[ADMIN_ID] = True
    bot.reply_to(message, "📢 **Broadcast Mode Activated!**\n\nAb aap jo bhi bhejenge (Video, Photo, Audio, Document, Text - sath me Link ya Button ke sath), woh sare users ko broadcast kar diya jayega.\n\nCancel karne ke liye `/cancel` bhejein.")


@bot.message_handler(commands=["cancel"])
def cancel_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return
    if ADMIN_ID in broadcast_states:
        del broadcast_states[ADMIN_ID]
        bot.reply_to(message, "❌ Broadcast mode cancelled.")
    else:
        bot.reply_to(message, "⚠️ No active broadcast process.")


@bot.message_handler(func=lambda msg: msg.from_user.id == ADMIN_ID and broadcast_states.get(ADMIN_ID, False), content_types=["text", "photo", "video", "audio", "document", "animation", "voice"])
def execute_broadcast(message):
    if message.text and message.text.startswith("/cancel"):
        return

    # Clear broadcast state immediately
    broadcast_states.pop(ADMIN_ID, None)
    
    users = get_all_users()
    success = 0
    failed = 0

    status_msg = bot.reply_to(message, f"🚀 Broadcast started to {len(users)} users...")

    for uid in users:
        try:
            # Forward or copy any media/text type with its buttons/links intact
            bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.id)
            success += 1
            time.sleep(0.05) # Prevent flood limits
        except Exception:
            failed += 1

    try:
        bot.edit_message_text(
            f"✅ **Broadcast Completed!**\n\n• Successful: {success}\n• Failed: {failed}",
            chat_id=message.chat.id,
            message_id=status_msg.id,
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ============ START COMMAND WITH ADMIN PANEL ============
@bot.message_handler(commands=["start"])
def send_welcome(message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name or "User"
        add_user(user_id, first_name)

        if user_id == ADMIN_ID:
            text = (
                "👑 **Admin Control Panel**\n\n"
                "• /broadcast - Saare users ko Media/Video/Audio/Link broadcast karein\n"
                "• Reply message + `/log` - Kisi message ko blacklist/auto-delete karne ke liye\n"
                "• Bot active hai aur saare security/reaction features background me chal rahe hain."
            )
        else:
            text = "🤖 **Bot is online and active!**\nGroup protection & auto-reactions are running."

        bot.reply_to(message, text, parse_mode="Markdown")
    except Exception:
        logger.exception("Start command failed")


# ============ MAIN ============
def main():
    init_db()

    threading.Thread(
        target=start_health_server, daemon=True, name="health-server"
    ).start()

    start_self_ping()
    logger.info("Starting Telegram polling...")

    while TypeGuard := True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=25,
                allowed_updates=["message", "channel_post", "edited_message", "edited_channel_post"],
                skip_pending=False
            )
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
            break
        except Exception:
            logger.exception("Telegram polling failed; retrying in 5 seconds")
            time.sleep(5)


if __name__ == "__main__":
    main()
