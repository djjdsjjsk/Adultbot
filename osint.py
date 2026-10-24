import os
import time
import sqlite3
import threading
import logging
import requests
import telebot

from datetime import timedelta
from telegram import ChatPermissions
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ============ CONFIGURATION ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "https://adultbot-k29i.onrender.com").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
DB_PATH = os.environ.get("DB_PATH", "bot_data.db")

# यहाँ अपने प्राइवेट ग्रुप या लॉग चैनल की ID डालें जहाँ सारे मैसेज फॉरवर्ड होंगे (जैसे: -1003907426148)
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0")) 

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
    raise RuntimeError("BOT_TOKEN missing. Render Dashboard mein BOT_TOKEN set karein.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("telegram-bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)


# ============ DATABASE HELPERS ============
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def init_db():
    conn = get_db()
    try:
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_settings WHERE key = 'mute_duration'")
        if not cursor.fetchone():
            conn.execute("INSERT INTO bot_settings (key, value) VALUES ('mute_duration', '900')")
        conn.commit()
    finally:
        conn.close()

def get_mute_duration():
    conn = get_db()
    try:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = 'mute_duration'").fetchone()
        return int(row[0]) if row else 900
    finally:
        conn.close()

def set_mute_duration(seconds):
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('mute_duration', ?)", (str(seconds),))
        conn.commit()
    finally:
        conn.close()

def is_message_logged(content_hash):
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM logged_messages WHERE content_hash = ?", (content_hash,)).fetchone()
        return row is not None
    finally:
        conn.close()

def log_message_hash(content_hash):
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO logged_messages (content_hash) VALUES (?)", (content_hash,))
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

def get_all_logged_messages(chat_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        prefix = f"{chat_id}_"
        cursor.execute("SELECT content_hash FROM logged_messages WHERE content_hash LIKE ?", (prefix + "%",))
        results = []
        for row in cursor.fetchall():
            clean_text = row[0][len(prefix):]
            results.append(clean_text)
        return results
    finally:
        conn.close()

def check_and_update_spam(user_id, content_hash):
    conn = get_db()
    now = time.time()
    mute_duration = get_mute_duration()
    try:
        row = conn.execute("SELECT last_msg_hash, count, mute_until FROM user_spam WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            last_hash, count, mute_until = row
            if now < mute_until:
                return "muted"

            if last_hash == content_hash:
                count += 1
            else:
                count = 1
                last_hash = content_hash

            if count >= 3:
                mute_until = now + mute_duration
                conn.execute("UPDATE user_spam SET last_msg_hash = ?, count = ?, mute_until = ? WHERE user_id = ?", (last_hash, count, mute_until, user_id))
                conn.commit()
                return "just_muted"
            else:
                conn.execute("UPDATE user_spam SET last_msg_hash = ?, count = ?, mute_until = 0 WHERE user_id = ?", (last_hash, count, user_id))
                conn.commit()
                return "active"
        else:
            conn.execute("INSERT INTO user_spam (user_id, last_msg_hash, count, mute_until) VALUES (?, ?, 1, 0)", (user_id, content_hash))
            conn.commit()
            return "active"
    finally:
        conn.close()

# ============ DYNAMIC ADMIN MANAGEMENT ============
def add_bot_admin(user_id):
    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO bot_admins (user_id) VALUES (?)", (user_id,))
        conn.commit()
    finally:
        conn.close()

def remove_bot_admin(user_id):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bot_admins WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

def is_bot_admin(user_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None
    finally:
        conn.close()

def is_group_admin(chat_id, user_id):
    if is_bot_admin(user_id):
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return True


# ============ WEB SERVER FOR RENDER ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, fmt, *args):
        pass

def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

def start_self_ping():
    def worker():
        while True:
            try:
                requests.get(APP_URL + "/", timeout=15)
            except Exception:
                pass
            time.sleep(180)
    threading.Thread(target=worker, daemon=True).start()


# ============ MESSAGE FILTER & FORWARD HANDLER ============
@bot.message_handler(func=lambda message: message.chat.type in ["group", "supergroup"] and (not message.text or not message.text.startswith("/")))
def handle_group_messages(message):
    try:
        chat_id = message.chat.id
        user_id = message.from_user.id if message.from_user else None

        # 1. Agar message bhejne wala admin hai toh ignore karein aur private log channel me forward karein
        if user_id and is_group_admin(chat_id, user_id):
            if LOG_CHANNEL_ID != 0:
                try:
                    bot.forward_message(chat_id=LOG_CHANNEL_ID, from_chat_id=chat_id, message_id=message.id)
                except Exception:
                    pass
            return

        text = (message.text or message.caption or "").lower().strip()
        if not text:
            return

        content_hash = f"{chat_id}_{text}"

        # 2. Check karein ki kya ye message pehle se logged/blocked hai
        if is_message_logged(content_hash):
            spam_status = check_and_update_spam(user_id, content_hash)
            try:
                bot.delete_message(chat_id=chat_id, message_id=message.id)
                if spam_status == "just_muted":
                    mute_secs = get_mute_duration()
                    mute_mins = int(mute_secs / 60)
                    bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=int(time.time() + mute_secs)
                    )
                    warn = bot.send_message(chat_id, f"⚠️ @{message.from_user.username or message.from_user.first_name}, locked message baar-baar bhejne ke karan aapko {mute_mins} minute ke liye mute kar diya gaya hai!")
                    threading.Thread(target=auto_delete_after_delay, args=(chat_id, [warn.id], 10)).start()
            except Exception:
                pass
        else:
            # Agar naya message hai aur logged nahi hai, toh use private log channel me forward kar denge taaki aap wahan se log kar sakein
            if LOG_CHANNEL_ID != 0:
                try:
                    bot.forward_message(chat_id=LOG_CHANNEL_ID, from_chat_id=chat_id, message_id=message.id)
                except Exception:
                    pass

    except Exception:
        logger.exception("Error in filter")


# ============ COMMANDS (/log, /unlock, /addlink, /dellink, /listlinks, /setmutetime) ============
def auto_delete_after_delay(chat_id, message_ids, delay=5):
    time.sleep(delay)
    for msg_id in message_ids:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

@bot.message_handler(commands=["addlink"])
def add_link_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            sent = bot.reply_to(message, "⚠️ Sahi tarika: `/addlink <link_or_text>` likhein.")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        link_text = args[1].lower().strip()
        content_hash = f"{chat_id}_{link_text}"
        log_message_hash(content_hash)

        sent = bot.reply_to(message, f"✅ Link/Word successfully added to lock list:\n`{link_text}`")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 6)).start()
    except Exception:
        logger.exception("Add link error")

@bot.message_handler(commands=["dellink"])
def del_link_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            sent = bot.reply_to(message, "⚠️ Sahi tarika: `/dellink <link_or_text>` likhein.")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        link_text = args[1].lower().strip()
        content_hash = f"{chat_id}_{link_text}"
        unlocked = unlock_message_hash(content_hash)

        if unlocked:
            sent = bot.reply_to(message, f"🗑️ Link/Word list se hata diya gaya hai:\n`{link_text}`")
        else:
            sent = bot.reply_to(message, "ℹ️ Yeh link/word list me nahi mila.")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 6)).start()
    except Exception:
        logger.exception("Del link error")

@bot.message_handler(commands=["listlinks"])
def list_links_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        links = get_all_logged_messages(chat_id)
        if not links:
            sent = bot.reply_to(message, "ℹ️ List me abhi koi link ya word added nahi hai.")
        else:
            links_str = "\n".join([f"• `{l}`" for l in links])
            sent = bot.reply_to(message, f"📋 **Total Locked Links/Words ({len(links)}):**\n\n{links_str}")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 15)).start()
    except Exception:
        logger.exception("List links error")

@bot.message_handler(commands=["setmutetime"])
def set_mute_time_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        args = message.text.split()
        if len(args) < 2:
            current_mins = int(get_mute_duration() / 60)
            sent = bot.reply_to(message, f"⏱️ Current Mute Time: **{current_mins} minutes**\n\nBadalne ka tarika: `/setmutetime <minutes>` (Jaise: `/setmutetime 20`)")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 8)).start()
            return

        try:
            mins = int(args[1])
            if mins <= 0:
                raise ValueError()
        except ValueError:
            sent = bot.reply_to(message, "⚠️ Kripya valid number dalein (Jaise: `/setmutetime 25`).")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        seconds = mins * 60
        set_mute_duration(seconds)
        sent = bot.reply_to(message, f"✅ Mute time successfully updated to **{mins} minutes**!")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 6)).start()
    except Exception:
        logger.exception("Set mute time error")

@bot.message_handler(commands=["log"])
def log_command(message):
    try:
        chat_id = message.chat.id
        # Allow /log command both in group or private log channel when replying to a message
        if not message.reply_to_message:
            sent = bot.reply_to(message, "⚠️ Kisi message par reply karke `/log` likhein.")
            if chat_id != LOG_CHANNEL_ID:
                threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        replied = message.reply_to_message
        text = (replied.text or replied.caption or "").lower().strip()
        if text:
            # Agar log channel se reply kiya gaya hai, toh original chat_id nikalne ke liye forward_from_chat check kar sakte hain, ya current chat_id use hoga
            target_chat_id = chat_id
            content_hash = f"{target_chat_id}_{text}"
            log_message_hash(content_hash)

            try:
                bot.delete_message(chat_id, replied.id)
            except Exception:
                pass

            sent = bot.reply_to(message, "🔒 **Post Locked & Deleted from Log Channel!**")
            if chat_id != LOG_CHANNEL_ID:
                threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
        else:
            sent = bot.reply_to(message, "⚠️ Sirf text messages ko lock kiya ja sakta hai.")
            if chat_id != LOG_CHANNEL_ID:
                threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
    except Exception:
        logger.exception("Log error")

@bot.message_handler(commands=["unlock"])
def unlock_command(message):
    try:
        chat_id = message.chat.id
        if not message.reply_to_message:
            sent = bot.reply_to(message, "⚠️ Kisi locked message par reply karke `/unlock` likhein.")
            return

        replied = message.reply_to_message
        text = (replied.text or replied.caption or "").lower().strip()
        if text:
            content_hash = f"{chat_id}_{text}"
            unlocked = unlock_message_hash(content_hash)
            if unlocked:
                sent = bot.reply_to(message, "✅ **Message Unlocked successfully!**")
            else:
                sent = bot.reply_to(message, "ℹ️ Yeh message pehle se locked nahi tha.")
        else:
            sent = bot.reply_to(message, "⚠️ Sirf text messages ke liye valid hai.")
    except Exception:
        logger.exception("Unlock error")

@bot.message_handler(commands=["addadmin"])
def add_admin_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        target_user_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user_id = message.reply_to_message.from_user.id
        else:
            args = message.text.split()
            if len(args) > 1:
                try:
                    target_user_id = int(args[1])
                except ValueError:
                    pass

        if not target_user_id:
            sent = bot.reply_to(message, "⚠️ Kisi user ke message par reply karein ya `/addadmin <user_id>` likhein.")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        add_bot_admin(target_user_id)
        sent = bot.reply_to(message, f"✅ User ID `{target_user_id}` ko successfully bot admin/protected bana diya gaya hai!")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
    except Exception:
        logger.exception("Add admin error")

@bot.message_handler(commands=["deladmin"])
def del_admin_command(message):
    try:
        chat_id = message.chat.id
        if message.chat.type == "private":
            return

        target_user_id = None
        if message.reply_to_message and message.reply_to_message.from_user:
            target_user_id = message.reply_to_message.from_user.id
        else:
            args = message.text.split()
            if len(args) > 1:
                try:
                    target_user_id = int(args[1])
                except ValueError:
                    pass

        if not target_user_id:
            sent = bot.reply_to(message, "⚠️ Kisi user ke message par reply karein ya `/deladmin <user_id>` likhein.")
            threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
            return

        removed = remove_bot_admin(target_user_id)
        if removed:
            sent = bot.reply_to(message, f"🗑️ User ID `{target_user_id}` ko admin list se hata diya gaya hai.")
        else:
            sent = bot.reply_to(message, "ℹ️ Yeh user list me nahi mila.")
        threading.Thread(target=auto_delete_after_delay, args=(chat_id, [message.id, sent.id], 5)).start()
    except Exception:
        logger.exception("Del admin error")


@bot.message_handler(commands=["start"])
def start_cmd(message):
    mute_mins = int(get_mute_duration() / 60)
    bot.reply_to(message, f"🤖 **Lock & Anti-Spam Bot Active!**\n\n• Reply + `/log` (Lock & Delete)\n• Reply + `/unlock` (Unlock)\n• `/addlink <text>` (Add Link/Word)\n• `/dellink <text>` (Remove Link/Word)\n• `/listlinks` (Show All Locked Links)\n• `/setmutetime <mins>` (Current: {mute_mins} mins)\n• `/addadmin` / `/deladmin` (Manage Admin)")


# ============ MAIN ============
def main():
    init_db()
    threading.Thread(target=start_health_server, daemon=True).start()
    start_self_ping()
    logger.info("Bot polling started...")
    
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception:
            time.sleep(5)

if __name__ == "__main__":
    main()
            
