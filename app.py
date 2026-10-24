import os
import sqlite3
import threading
import telebot
from flask import Flask, render_template, request, send_from_directory

app = Flask(__name__)

# टेलीग्राम बोट टोकन (इसे Render के Environment Variables में भी सेट कर सकते हैं)
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

DB_FILE = "database.sqlite"
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def init_db():
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_name TEXT,
            version TEXT,
            file_size TEXT,
            package_name TEXT,
            icon_url TEXT,
            file_url TEXT,
            description TEXT
        )
    """)
  conn.commit()
  conn.close()


init_db()


# --- वेबसाइट रूट्स ---
@app.route("/")
def index():
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute("SELECT * FROM apps ORDER BY id DESC LIMIT 1")
  app_data = cursor.fetchone()
  conn.close()

  if app_data:
    app_info = {
        "app_name": app_data[1],
        "version": app_data[2],
        "file_size": app_data[3],
        "icon_url": app_data[5] if app_data[5] else "https://via.placeholder.com/150",
        "file_url": f"/download/{app_data[1]}",
        "description": app_data[7],
    }
  else:
    app_info = {
        "app_name": "OTT App 3.44.0.apk",
        "version": "3.44.0",
        "file_size": "221.1MB",
        "icon_url": "https://via.placeholder.com/150",
        "file_url": "#",
        "description": "This is an amazing OTT streaming application.",
    }

  return render_template("index.html", app=app_info)


@app.route("/download/<path:filename>")
def download_file(filename):
  return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


# --- टेलीग्राम बोट हैंडलर ---
@bot.message_handler(commands=["start"])
def send_welcome(message):
  bot.reply_to(
      message, "नमस्ते! वेबसाइट पर नया ऐप जोड़ने के लिए मुझे APK फाइल भेजें।"
  )


@bot.message_handler(content_types=["document"])
def handle_docs(message):
  try:
    file_info = bot.get_file(message.document.file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    file_name = message.document.file_name
    file_path = os.path.join(UPLOAD_FOLDER, file_name)

    with open(file_path, "wb") as new_file:
      new_file.write(downloaded_file)

    file_size_mb = f"{round(message.document.file_size / (1024 * 1024), 1)}MB"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO apps (app_name, version, file_size, icon_url, file_url, description)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (
            file_name,
            "1.0",
            file_size_mb,
            "https://via.placeholder.com/150",
            file_path,
            "Uploaded via Telegram Bot.",
        ),
    )
    conn.commit()
    conn.close()

    bot.reply_to(
        message,
        f"✅ ऐप सफलतापूर्वक अपलोड हो गया!\n📦 नाम: {file_name}\n💾 साइज:"
        f" {file_size_mb}",
    )
  except Exception as e:
    bot.reply_to(message, f"❌ एरर आ गया: {str(e)}")


# बोट को बैकग्राउंड में चलाने के लिए
def run_bot():
  bot.infinity_polling()


if __name__ == "__main__":
  # बोट को अलग धागे (thread) में शुरू करें
  t = threading.Thread(target=run_bot)
  t.daemon = True
  t.start()

  # Flask ऐप शुरू करें
  app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
  