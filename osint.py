import re
import threading
import time
from flask import Flask
from telebot import TeleBot, types

# --- Flask सर्वर (Render पर 24/7 एक्टिव रखने के लिए) ---
app = Flask(__name__)


@app.route("/")
def home():
  return "Bot is active and running 24/7!"


def run_flask():
  # Render अपने हिसाब से PORT असाइन करता है, डिफ़ॉल्ट 5000 है
  app.run(host="0.0.0.0", port=5000)


# --- Keep-Alive पििंगर (हर 2-3 मिनट में खुद की लिंक दबाने के लिए) ---
import urllib.request


def keep_alive_ping():
  url = "https://adultbot-k29i.onrender.com"
  while True:
    try:
      urllib.request.urlopen(url)
      print("Pinged successfully to keep alive!")
    except Exception as e:
      print(f"Ping failed: {e}")
    # हर 2 मिनट (120 सेकंड) में पिंग करेगा
    time.sleep(120)


# --- Telegram Bot Setup ---
TOKEN = "YOUR_BOT_TOKEN_HERE"
bot = TeleBot(TOKEN)

# 1. URL Whitelist (यहाँ अपने भरोसेमंद लिंक्स/डोमेन जोड़ें जो बैन नहीं होंगे)
WHITELISTED_URLS = [
    "youtube.com",
    "t.me/your_channel_username",  # आपके चैनल का यूजरनेम यहाँ डालें
    "example.com",
    "adultbot-k29i.onrender.com",  # आपकी खुद की होस्टिंग लिंक भी जोड़ दी गई है
]

# यूजर्स की वार्निंग्स ट्रैक करने के लिए डिक्शनरी
user_warnings = {}


# फंक्शन: चेक करना कि मैसेज में कोई unlisted लिंक है या नहीं
def contains_unlisted_url(text):
  if not text:
    return False

  # URL पकड़ने का रेगुलर एक्सप्रेसिन
  url_pattern = r"(https?://\S+|www\.\S+|t\.me/\S+)"
  urls = re.findall(url_pattern, text)
  if not urls:
    return False

  for url in urls:
    is_whitelisted = False
    for w in WHITELISTED_URLS:
      if w.lower() in url.lower():
        is_whitelisted = True
        break
    # अगर कोई ऐसा URL मिला जो whitelist में नहीं है
    if not is_whitelisted:
      return True

  return False


# 1. /start और /help कमांड: एडमिन के लिए सभी कमांड्स की लिस्ट दिखाना
@bot.message_handler(commands=['start', 'help'])
def show_help(message):
  try:
    chat_member = bot.get_chat_member(message.chat.id, message.from_user.id)
    if chat_member.status not in ['administrator', 'creator']:
      bot.reply_to(
          message, '❌ यह कमांड लिस्ट सिर्फ ग्रुप एडमिन के लिए उपलब्ध है।'
      )
      return
  except Exception:
    return

  help_text = (
      '🤖 **ग्रुप मॉडेशन बोट - एडमिन कमांड्स गाइड**\n\n'
      'नीचे बोट के सभी उपलब्ध कमांड्स और उनके काम दिए गए हैं:\n\n'
      '🔹 `/help` या `/start`\n'
      '   👉 बोट के सभी कमांड्स की यह लिस्ट देखने के लिए।\n\n'
      '🔹 `/unrestrict <User_ID>` (या `/unmute`) \n'
      '   👉 किसी म्यूट किए गए यूजर की आईडी डालकर उसका रिस्ट्रिक्शन तुरंत'
      ' हटाने के लिए।\n\n'
      '📌 **एंटी-लिंक सिस्टम नियम:**\n'
      '• बिना अनुमति (Whitelist) वाले लिंक भेजने पर पहली और दूसरी बार मैसेज'
      ' डिलीट कर वार्निंग दी जाएगी।\n'
      '• तीसरी बार लिंक भेजने पर यूजर ऑटोमैटिकली **15 मिनट** के लिए म्यूट'
      ' (Restrict) हो जाएगा।'
  )

  bot.reply_to(message, help_text, parse_mode='Markdown')


# 2. /unrestrict कमांड: यूजर को अनम्यूट करने के लिए
@bot.message_handler(commands=['unrestrict', 'unmute'])
def unrestrict_user(message):
  try:
    chat_member = bot.get_chat_member(message.chat.id, message.from_user.id)
    if chat_member.status not in ['administrator', 'creator']:
      bot.reply_to(message, '❌ यह कमांड सिर्फ ग्रुप एडमिन के लिए है!')
      return
  except Exception:
    return

  args = message.text.split()

  if len(args) < 2:
    bot.reply_to(
        message,
        '⚠️ **इस्तेमाल करने का तरीका गलत है!**\n\nसही तरीका:\n`/unrestrict'
        ' <User_ID>`\nउदाहरण: `/unrestrict 8774983792`',
        parse_mode='Markdown',
    )
    return

  try:
    target_user_id = int(args[1])
    bot.restrict_chat_member(
        message.chat.id,
        target_user_id,
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

    if target_user_id in user_warnings:
      user_warnings[target_user_id] = 0

    bot.reply_to(
        message,
        f'✅ सफलता! यूजर (ID: `{target_user_id}`) का रिस्ट्रिक्शन हटा दिया'
        ' गया है।',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.reply_to(
        message,
        f'❌ अनरिस्ट्रिक्ट करने में एरर आया: {e}\n(सुनिश्चित करें कि बोट एडमिन'
        ' है और आईडी सही है)',
    )


# 3. सामान्य मैसेज मॉनिटर: लिंक चेक करने और 3 स्ट्राइक्स पर म्यूट करने के लिए
@bot.message_handler(func=lambda message: True, content_types=['text'])
def monitor_links(message):
  if message.from_user:
    try:
      member = bot.get_chat_member(message.chat.id, message.from_user.id)
      if member.status in ['administrator', 'creator']:
        return
    except:
      pass

  if message.text and contains_unlisted_url(message.text):
    user_id = message.from_user.id
    user_name = message.from_user.first_name

    try:
      bot.delete_message(message.chat.id, message.message_id)
    except:
      pass

    user_warnings[user_id] = user_warnings.get(user_id, 0) + 1
    warnings = user_warnings[user_id]

    if warnings < 3:
      bot.send_message(
          message.chat.id,
          f'⚠️ **{user_name}**, इस ग्रुप में बिना अनुमति के लिंक भेजना मना'
          f' है!\nचेतावनी ({warnings}/3): आपका मैसेज डिलीट कर दिया गया है। 3'
          ' बार ऐसा करने पर 15 मिनट के लिए म्यूट कर दिए जाओगे।',
          parse_mode='Markdown',
      )
    else:
      until_time = int(time.time()) + 900
      try:
        bot.restrict_chat_member(
            message.chat.id,
            user_id,
            until_date=until_time,
            can_send_messages=False,
        )
        bot.send_message(
            message.chat.id,
            f'🚫 **{user_name}** (ID: `{user_id}`) को 3 बार अनधिकृत लिंक भेजने के'
            ' कारण **15 मिनट** के लिए म्यूट कर दिया गया है!',
            parse_mode='Markdown',
        )
        user_warnings[user_id] = 0
      except Exception as e:
        print(f'Restriction error: {e}')


# --- मुख्य प्रोग्राम एग्जीक्यूशन ---
if __name__ == "__main__":
  # 1. Flask सर्वर को बैकग्राउंड धागे (Thread) में चलाना
  t_flask = threading.Thread(target=run_flask)
  t_flask.daemon = True
  t_flask.start()

  # 2. Keep-Alive पिंगर को बैकग्राउंड में चलाना (हर 2 मिनट में लिंक को हिट करेगा)
  t_ping = threading.Thread(target=keep_alive_ping)
  t_ping.daemon = True
  t_ping.start()

  # 3. बोट पोलिंग शुरू करना
  print("Bot and Keep-Alive server are running...")
  bot.infinity_polling()
    
