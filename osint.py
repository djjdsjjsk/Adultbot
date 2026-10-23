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
STORAGE_CHANNEL = int(os.environ.get("STORAGE_CHANNEL", -1001234567890))  # प्राइवेट स्टोरेज चैनल की आईडी (माइनस के साथ)
# =================================================

# MongoDB Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["file_sharing_bot"]
users_col = db["users"]
files_col = db["files"]

# Flask App for 24/7 Uptime on Render
app = Flask(__name__)

@app.route('/')
def home():
    return "File Sharing Bot is Running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Pyrogram Bot Client (सिर्फ बोट टोकन से चलेगा)
bot = Client("file_sharing_bot", bot_token=BOT_TOKEN)

# --- START COMMAND & FILE DEEP LINKING ---
@bot.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    user_id = message.from_user.id
    
    # यूजर को डेटाबेस में सेव करें (ब्रॉडकास्ट के लिए)
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id})

    # चेक करें कि क्या यूजर ने किसी फाइल के लिंक से स्टार्ट किया है (Deep Linking)
    if len(message.command) > 1:
        file_id = message.command[1]
        file_data = files_col.find_one({"file_code": file_id})
        
        if file_data:
            # स्टोरेज चैनल से उस मैसेज को यूजर के पास कॉपी/फॉरवर्ड कर दें (एग्जैक्ट सेम टू सेम)
            await client.copy_message(
                chat_id=user_id,
                from_chat_id=STORAGE_CHANNEL,
                message_id=file_data["message_id"],
                caption=file_data.get("caption")
            )
            return
        else:
            await message.reply("❌ यह फाइल या लिंक मौजूद नहीं है या डिलीट कर दी गई है।")
            return

    # सामान्य /start कमांड
    if user_id == ADMIN_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 ब्रॉडकास्ट (Broadcast)", callback_data="broadcast_start")],
            [InlineKeyboardButton("📊 कुल यूजर्स देखें", callback_data="total_users")]
        ])
        await message.reply("👑 **नमस्ते एडमिन!** आपके कंट्रोल पैनल में आपका स्वागत है:", reply_markup=keyboard)
    else:
        await message.reply(
            "👋 नमस्ते! मैं एक फाइल शेयरिंग बोट हूँ।\n\n"
            "आप मुझे कोई भी फाइल, वीडियो, फोटो या टेक्स्ट भेजें, मैं आपको उसका **शेयरेबल लिंक** दे दूंगा!"
        )

# --- RECEIVE FILE FROM USER & GENERATE LINK ---
@bot.on_message(filters.private & ~filters.command(["start"]) & ~filters.user(ADMIN_ID))
async def store_file_handler(client, message: Message):
    # यूजर द्वारा भेजी गई फाइल/मैसेज को प्राइवेट स्टोरेज चैनल में फॉरवर्ड करें
    forwarded_msg = await message.forward(chat_id=STORAGE_CHANNEL)
    
    # एक यूनिक फाइल कोड बनाएं
    file_code = str(forwarded_msg.id)
    
    # डेटाबेस में स्टोर करें
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
        f"✅ **आपकी फाइल सफलतापूर्वक सेव हो गई है!**\n\n"
        f"🔗 **शेयरेबल लिंक:**\n`{share_link}`",
        disable_web_page_preview=True
    )

# --- ADMIN BROADCAST SYSTEM ---
broadcast_state = {}

@bot.on_callback_query(filters.regex("broadcast_start"))
async def broadcast_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    broadcast_state[ADMIN_ID] = True
    await callback.message.reply("📢 कृपया वह मैसेज (टेक्स्ट, फोटो या वीडियो) भेजें जिसे आप सभी यूजर्स को ब्रॉडकास्ट करना चाहते हैं:")

@bot.on_message(filters.private & filters.user(ADMIN_ID) & ~filters.command(["start"]))
async def admin_broadcast_sender(client, message: Message):
    if broadcast_state.get(ADMIN_ID):
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
            f"❌ असफल (बोट ब्लॉक करने वाले): `{fail_count}`"
        )

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
    
