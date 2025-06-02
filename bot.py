import logging
import time
import random
import string
import os
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from flask import Flask, request
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes
import aiohttp
import threading
import asyncio
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
SHORTNER_API = os.getenv("SHORTNER_API")
FLASK_URL = os.getenv("FLASK_URL")
LIKE_API_URL = os.getenv("LIKE_API_URL")
HOW_TO_VERIFY_URL = os.getenv("HOW_TO_VERIFY_URL")
VIP_ACCESS_URL = os.getenv("VIP_ACCESS_URL")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.isdigit()]

DAILY_REQUEST_LIMIT = 1
REQUEST_RESET_HOURS = 20

# MongoDB setup
client = AsyncIOMotorClient(MONGO_URI)
db = client['likebot']
users = db['verifications']
profiles = db['users']
requests = db['requests']

# Flask app setup
flask_app = Flask(__name__)

@flask_app.route("/verify/<code>")
def verify(code):
    try:
        user = asyncio.run(users.find_one({"code": code}))
        if user and not user.get("verified"):
            asyncio.run(users.update_one(
                {"code": code}, 
                {"$set": {"verified": True, "verified_at": datetime.utcnow()}}
            ))
            return "✅ Verification successful. Bot will now process your like."
        return "❌ Link expired or already used."
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return "❌ An error occurred during verification."

async def check_user_requests(user_id):
    try:
        if user_id in ADMIN_IDS:
            return float('inf')
        
        user_request = await requests.find_one({"user_id": user_id})
        if not user_request:
            return DAILY_REQUEST_LIMIT
            
        last_request_time = user_request.get("last_request_time")
        if not last_request_time:
            return DAILY_REQUEST_LIMIT
            
        time_since_last_request = datetime.utcnow() - last_request_time
        if time_since_last_request > timedelta(hours=REQUEST_RESET_HOURS):
            return DAILY_REQUEST_LIMIT
            
        return user_request.get("remaining_requests", DAILY_REQUEST_LIMIT)
    except Exception as e:
        logger.error(f"Error checking user requests: {e}")
        return DAILY_REQUEST_LIMIT

async def update_user_requests(user_id):
    try:
        if user_id in ADMIN_IDS:
            return True
            
        current_requests = await check_user_requests(user_id)
        if current_requests <= 0:
            return False
            
        await requests.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "last_request_time": datetime.utcnow(),
                    "remaining_requests": current_requests - 1
                }
            },
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error updating user requests: {e}")
        return False

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.effective_user.id
        remaining_requests = await check_user_requests(user_id)
        profile = await profiles.find_one({"user_id": user_id}) or {}
        vip_expires = profile.get("vip_expires")
        is_vip = vip_expires and datetime.utcnow() < vip_expires
        
        if user_id in ADMIN_IDS:
            await update.message.reply_text("👑 *Admin Status*\n\nYou have unlimited requests!", parse_mode='Markdown')
        elif is_vip:
            await update.message.reply_text("🌟 *VIP Status*\n\nUnlimited requests!", parse_mode='Markdown')
        else:
            await update.message.reply_text(
                f"📊 *Your Request Status*\n\n"
                f"📅 Requests left: {remaining_requests}/{DAILY_REQUEST_LIMIT}\n"
                f"⏳ Resets every {REQUEST_RESET_HOURS} hours",
                parse_mode='Markdown'
            )
    except Exception as e:
        logger.error(f"Error in check command: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request.")

async def like_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    try:
        user_id = update.effective_user.id
        username = update.message.from_user.first_name or "User"
        profile = await profiles.find_one({"user_id": user_id}) or {}
        vip_expires = profile.get("vip_expires")
        is_vip = vip_expires and datetime.utcnow() < vip_expires
        is_admin = user_id in ADMIN_IDS
        
        if not is_vip and not is_admin:
            remaining_requests = await check_user_requests(user_id)
            if remaining_requests <= 0:
                await update.message.reply_text("🚫 Daily request limit reached.", parse_mode='Markdown')
                return
                
        try:
            args = update.message.text.split()
            if len(args) < 3:
                raise ValueError("Insufficient arguments")
            region = args[1].lower()
            uid = args[2]
        except:
            await update.message.reply_text("❌ Format: /like <region> <uid>")
            return
            
        if is_admin or is_vip:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(LIKE_API_URL.format(uid=uid, region=region), timeout=10) as resp:
                        if resp.status == 200:
                            api_resp = await resp.json()
                            if api_resp.get("status") == 1:
                                player_nickname = api_resp.get("PlayerNickname", "Unknown")
                                before = api_resp.get("LikesbeforeCommand", 0)
                                after = api_resp.get("LikesafterCommand", 0)
                                added = api_resp.get("LikesGivenByAPI", 0)
                                result = (f"✅ *Like Processed*\n👤 {player_nickname}\n🆔 {uid}\n👍 {before}->{after} (+{added})")
                                await profiles.update_one(
                                    {"user_id": user_id}, 
                                    {"$set": {"last_used": datetime.utcnow()}}, 
                                    upsert=True
                                )
                            elif api_resp.get("status") == 2:
                                result = "❌ Max likes for UID."
                            else:
                                result = "❌ API Error."
                        else:
                            result = f"❌ HTTP Error {resp.status}"
            except Exception as e:
                result = f"❌ Error: {str(e)}"
            await update.message.reply_text(result, parse_mode='Markdown')
            return
            
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://shortner.in/api?api={SHORTNER_API}&url={FLASK_URL}/verify/{code}"
                ) as r:
                    data = await r.json()
                    short_link = data.get("shortenedUrl", f"{FLASK_URL}/verify/{code}")
        except Exception as e:
            logger.error(f"Shortener error: {e}")
            short_link = f"{FLASK_URL}/verify/{code}"
            
        await users.insert_one({
            "user_id": user_id, 
            "uid": uid, 
            "region": region, 
            "code": code,
            "verified": False, 
            "expires_at": datetime.utcnow() + timedelta(minutes=10),
            "chat_id": update.effective_chat.id, 
            "message_id": update.message.message_id
        })
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ VERIFY", url=short_link)],
            [InlineKeyboardButton("❓ How to Verify", url=HOW_TO_VERIFY_URL)]
        ])
        
        await update.message.reply_text(
            f"🔒 *Verification Needed*\n🤵 {username}\n🆔 {uid}\n🌍 {region}\n"
            f"Verify via link below:\n{short_link}\nExpires in 10 mins\nVIP: {VIP_ACCESS_URL}",
            reply_markup=keyboard, 
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in like command: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request.")

async def addvip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("🚫 Not authorized.")
            return
            
        try:
            target_id = int(context.args[0])
            days = int(context.args[1])
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Format: /addvip <user_id> <days>")
            return
            
        expiration_date = datetime.utcnow() + timedelta(days=days)
        await profiles.update_one(
            {"user_id": target_id}, 
            {"$set": {"vip_expires": expiration_date}}, 
            upsert=True
        )
        await update.message.reply_text(
            f"✅ VIP for {target_id} till {expiration_date}", 
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Error in addvip command: {e}")
        await update.message.reply_text("❌ An error occurred while processing your request.")

async def process_verified_likes(app: Application):
    while True:
        try:
            async for user in users.find({"verified": True, "processed": {"$ne": True}}):
                try:
                    uid = user['uid']
                    region = user.get('region', 'ind')
                    user_id = user['user_id']
                    profile = await profiles.find_one({"user_id": user_id}) or {}
                    vip_expires = profile.get("vip_expires")
                    is_vip = vip_expires and datetime.utcnow() < vip_expires
                    is_admin = user_id in ADMIN_IDS
                    
                    if not is_vip and not is_admin:
                        if not await update_user_requests(user_id):
                            await app.bot.send_message(
                                user['chat_id'], 
                                reply_to_message_id=user['message_id'],
                                text="🚫 Daily limit reached.", 
                                parse_mode='Markdown'
                            )
                            await users.update_one({"_id": user['_id']}, {"$set": {"processed": True}})
                            continue
                            
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                LIKE_API_URL.format(uid=uid, region=region), 
                                timeout=10
                            ) as resp:
                                if resp.status == 200:
                                    api_resp = await resp.json()
                                    if api_resp.get("status") == 1:
                                        player_nickname = api_resp.get("PlayerNickname", "Unknown")
                                        before = api_resp.get("LikesbeforeCommand", 0)
                                        after = api_resp.get("LikesafterCommand", 0)
                                        added = api_resp.get("LikesGivenByAPI", 0)
                                        result = (f"✅ *Like Processed*\n👤 {player_nickname}\n🆔 {uid}\n👍 {before}->{after} (+{added})")
                                        await profiles.update_one(
                                            {"user_id": user_id}, 
                                            {"$set": {"last_used": datetime.utcnow()}}, 
                                            upsert=True
                                        )
                                    elif api_resp.get("status") == 2:
                                        result = "❌ Max likes for UID."
                                    else:
                                        result = "❌ API Error."
                                else:
                                    result = f"❌ HTTP Error {resp.status}"
                    except Exception as e:
                        result = f"❌ Error: {str(e)}"
                        
                    await app.bot.send_message(
                        user['chat_id'], 
                        reply_to_message_id=user['message_id'], 
                        text=result, 
                        parse_mode='Markdown'
                    )
                    await users.update_one({"_id": user['_id']}, {"$set": {"processed": True}})
                except Exception as e:
                    logger.error(f"Error processing verified like: {e}")
                    
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in verified likes processor: {e}")
            await asyncio.sleep(10)

def run_flask():
    flask_app.run(host="0.0.0.0", port=5000)

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("like", like_command))
    app.add_handler(CommandHandler("addvip", addvip_command))
    app.add_handler(CommandHandler("check", check_command))
    
    # Run Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start the verified likes processor
    loop = asyncio.get_event_loop()
    loop.create_task(process_verified_likes(app))
    
    # Start the bot
    app.run_polling()

if __name__ == '__main__':
    run_bot()