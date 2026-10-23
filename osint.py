import os
import asyncio
from flask import Flask
from threading import Thread
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient

# ================= CONFIGURATION =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")  # बोट टोकन डालें
MONGO_URI = os.environ.get("MONGO_URI", "your_mongodb_connection_string")  # MongoDB URL
ADMIN_ID = int(os.environ.get("ADMIN_ID", 5989928539))  # आपकी एडमिन आईडी
# =================================================

# MongoDB Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_sharing_bot"]
users_col = db["users"]
files_col = db["files"]
settings_col = db["settings"]  # चैनल आईडी सेव करने के लिए

# Flask App for 24/7 Uptime on Render
app = Flask(__name__)

@app.route('/')
def home():
    return "File Sharing Bot is Running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Pyrogram Bot Client
bot = Client("file_sharing_bot", bot_token=BOT_TOKEN)

# स्टोरेज चैनल प्राप्त करने के लिए फंक्शन
def get_storage_channel():
    data = settings_col.find_one({"_id": "storage_channel"})
    return data["channel_id"] if data else None

# --- START COMMAND & FILE DEEP LINKING ---
@bot.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    user_id = message.from_user.id
    
    # यूजर को डेटाबेस में सेव करें
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id})

    # Deep Linking चेक करें (फाइल देखने के लिए)
    if len(message.command) > 1:
        file_id = message.command[1]
        file_data = files_col.find_one({"file_code": file_id})
        storage_channel = get_storage_channel()
        
        if file_data and storage_channel:
            try:
                await client.copy_message(
                    chat_id=user_id,
                    from_chat_id=storage_channel,
                    message_id=file_data["message_id"],
                    caption=file_data.get("caption")
                )
            except Exception as e:
                await message.reply(f"❌ फाइल भेजने में त्रुटि: {str(e)}")
            return
        else:
            await message.reply("❌ यह फाइल मौजूद नहीं है या स्टोरेज चैनल सेट नहीं है।")
            return

    # एडमिन पैनल
    if user_id == ADMIN_ID:
        current_channel = get_storage_channel()
        channel_text = f"`{current_channel}`" if current_channel else "⚠️ सेट नहीं है!"
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 ब्रॉडकास्ट (Broadcast)", callback_data="broadcast_start")],
            [InlineKeyboardButton("⚙️ स्टोरेज चैनल सेट करें", callback_data="set_channel_prompt")],
            [InlineKeyboardButton("📊 कुल यूजर्स देखें", callback_data="total_users")]
        ])
        await message.reply(
            f"👑 **नमस्ते एडमिन!**\n\n"
            f"📁 वर्तमान स्टोरेज चैनल: {channel_text}\n\n"
            f"कंट्रोल पैनल:", 
            reply_markup=keyboard
        )
    else:
        await message.reply(
            "👋 नमस्ते! मैं एक फाइल शेयरिंग बोट हूँ।\n\n"
            "आप मुझे कोई भी फाइल, वीडियो, फोटो या टेक्स्ट भेजें, मैं आपको उसका **शेयरेबल लिंक** दे दूंगा!"
        )

# --- ADMIN: SET STORAGE CHANNEL ---
set_channel_state = {}

@bot.on_callback_query(filters.regex("set_channel_prompt"))
async def set_channel_prompt_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    set_channel_state[ADMIN_ID] = True
    await callback.message.reply(
        "⚙️ कृपया अपने **प्राइवेट स्टोरेज चैनल की ID** (जैसे `-100xxxxxxxxxx`) यहाँ भेजें:\n\n"
        "(ध्यान रहे कि बोट उस चैनल में एडमिन होना चाहिए)"
    )

# --- RECEIVE FILE FROM USER & GENERATE LINK ---
@bot.on_message(filters.private & ~filters.command(["start"]))
async def message_router(client, message: Message):
    user_id = message.from_user.id
    
    # 1. अगर एडमिन चैनल आईडी सेट कर रहा है
    if user_id == ADMIN_ID and set_channel_state.get(ADMIN_ID):
        set_channel_state[ADMIN_ID] = False
        try:
            channel_id = int(message.text.strip())
            settings_col.update_one(
                {"_id": "storage_channel"},
                {"$set": {"channel_id": channel_id}},
                upsert=True
            )
            await message.reply(f"✅ स्टोरेज चैनल सफलताપूर्वक सेट हो गया: `{channel_id}`")
        except ValueError:
            await message.reply("❌ अमान्य चैनल आईडी! कृपया सिर्फ नंबर वाली आईडी भेजें (माइनस के साथ)।")
        return

    # 2. अगर एडमिन ब्रॉडकास्ट मैसेज भेज रहा है
    if user_id == ADMIN_ID and broadcast_state.get(ADMIN_ID):
        broadcast_state[ADMIN_ID] = False
        all_users = list(users_col.find())
        
        sent_count = 0
        fail_count = 0
        status_msg = await message.reply(f"🚀 ब्रॉडकास्ट शुरू हो गया है... कुल यूजर्स: {len(all_users)}")
        
        for user in all_users:
            try:
                await message.copy(chat_id=user["user_id"])
                sent_count += 1
                await asyncio.sleep(0.1)
            except Exception:
                fail_count += 1
                
        await status_msg.edit_text(
            f"✅ **ब्रॉडकास्ट पूरा हो गया!**\n\n"
            f"👥 कुल सफलता: `{sent_count}`\n"
            f"❌ असफल: `{fail_count}`"
        )
        return

    # 3. आम यूजर द्वारा फाइल भेजना और लिंक बनाना
    storage_channel = get_storage_channel()
    if not storage_channel:
        if user_id == ADMIN_ID:
            await message.reply("⚠️ पहले बोट में स्टोरेज चैनल सेट करें! /start दबाएं।")
        else:
            await message.reply("⚠️ बोट अभी कॉन्फ़िगर नहीं किया गया है। कृपया बाद में प्रयास करें।")
        return

    try:
        # फाइल को प्राइवेट चैनल में फॉरवर्ड करें
        forwarded_msg = await message.forward(chat_id=storage_channel)
        file_code = str(forwarded_msg.id)
        
        # डेटाबेस में सेव करें
        files_col.update_one(
            {"file_code": file_code},
            {"$set": {
                "message_id": forwarded_msg.id,
                "caption": message.caption or message.text
            }},
            upsert=True
        )
        
        bot_info = await client.get_me()
        share_link = f"https://t.me/{bot_info.username}?start={file_code}"
        
        await message.reply(
            f"✅ **फाइल सफलतापूर्वक सेव हो गई है!**\n\n"
            f"🔗 **शेयरेबल लिंक:**\n`{share_link}`",
            disable_web_page_preview=True
        )
    except Exception as e:
        await message.reply(f"❌ फाइल सेव करने में त्रुटि (शायद बोट चैनल में एडमिन नहीं है): {str(e)}")

# --- ADMIN BROADCAST TRIGGER ---
broadcast_state = {}

@bot.on_callback_query(filters.regex("broadcast_start"))
async def broadcast_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    broadcast_state[ADMIN_ID] = True
    await callback.message.reply("📢 कृपया वह मैसेज भेजें जिसे आप सभी यूजर्स को ब्रॉडकास्ट करना चाहते हैं:")

@bot.on_callback_query(filters.regex("total_users"))
async def total_users_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    count = users_col.count_documents({})
    await callback.answer(f"बोट के कुल यूजर्स: {count}", show_alert=True)

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    print("🤖 फाइल शेयरिंग बोट शुरू हो रहा है...")
    bot.run()
    
