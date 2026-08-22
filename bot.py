import re
import time
import json
import os
import logging
import asyncio
import random
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

# ============ الإعدادات الأساسية ============
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
ADMIN_ID = 643309456 # أيدي الإدارة

# روابط Jio
CHECK_NUMBER_URL = "https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{mobile}"
SEND_OTP_URL = "https://www.jio.com/api/jio-login-service/login/sendOtp"
VERIFY_OTP_URL = "https://www.jio.com/api/jio-login-service/login/validateOtp"
AUTH_URL = "https://www.jio.com/api/jio-authenticate-service/authenticate/authJsonData"
NAVIGATE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/navigate/Z0241"
ACTIVATE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/activate/Z0241?source=JIO"
GOOGLE_URL = "https://www.jio.com/api/jio-ott-service/ott/subscription/google-ai"
SUBMIT_URL = "https://www.jio.com/api/jio-ott-service/ott/submission/submit"
GOOGLE_PAGE = "https://www.jio.com/selfcare/googleai/?header=no&type=Z0241&source=JIO"
GRIZZLY_API_URL = "https://api.grizzlysms.com/stubs/handler_api.php"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ قاعدة بيانات المستخدمين ============
DB_FILE = "users_db.json"

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)

users_db = load_db()

class UserSession:
    def __init__(self, user_id, api_key):
        self.user_id = user_id
        self.api_key = api_key
        self.status = "stopped"
        self.active_workers = 0
        self.bought = 0
        self.otp_received = 0
        self.success_links = 0
        self.cancelled_timeout = 0
        self.invalid_jio = 0
        self.failed_send_otp = 0
        self.otp_rejected_jio = 0
        self.message_obj = None
        self.last_buy_time = time.time() # لحساب الوقت المار بدون شراء أرقام
        self.alert_sent = False # لمنع تكرار رسالة التنبيه

active_sessions = {}

# ============ دوال التأخير والتصفح ============
def human_delay(min_sec=1.5, max_sec=3.5):
    time.sleep(random.uniform(min_sec, max_sec))

async def async_human_delay(min_sec=2.0, max_sec=4.5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))

def create_jio_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jio.com",
        "Referer": "https://www.jio.com/selfcare/login/",
    })
    return session

def response_json(response: requests.Response) -> dict:
    try: return response.json()
    except ValueError: return {}

def response_error(response: requests.Response) -> bool:
    if not response.ok: return True
    data = response_json(response)
    return bool(data.get("errorMessage") or data.get("error"))

def check_jio_number(session: requests.Session, mobile: str) -> bool:
    try:
        response = session.get(CHECK_NUMBER_URL.format(mobile=mobile), timeout=20)
        return not response_error(response) and bool(response_json(response).get("primaryService"))
    except: return False

def send_otp(session: requests.Session, mobile: str) -> bool:
    try:
        response = session.post(SEND_OTP_URL, json={"mobileNumber": mobile, "loginFlowType": "MOBILE", "alternateNumber": ""}, timeout=20)
        return not response_error(response)
    except: return False

def verify_otp(session: requests.Session, mobile: str, otp: str) -> bool:
    try:
        response = session.post(VERIFY_OTP_URL, json={"mobileNumber": mobile, "otp": otp}, timeout=20)
        return not response_error(response)
    except: return False

def extract_activation(session: requests.Session) -> tuple[str, str]:
    dashboard_headers = {"Accept": "*/*", "Referer": "https://www.jio.com/selfcare/dashboard/"}
    offer_headers = {"Accept": "*/*", "Referer": GOOGLE_PAGE}
    try:
        human_delay(1.0, 2.5)
        auth_data = response_json(session.get(AUTH_URL, headers=dashboard_headers, timeout=20))
        if str(auth_data.get("loginFlag", "")).lower() != "true": return "api_session_invalid", ""

        human_delay(1.5, 3.0)
        session.get(NAVIGATE_URL, headers=dashboard_headers, timeout=20)

        human_delay(2.0, 4.0)
        activate_data = response_json(session.get(ACTIVATE_URL, headers=offer_headers, timeout=20))
        if str(activate_data.get("errorCode", "200")) != "200": return "activation_api_failed", ""

        human_delay(1.5, 3.5)
        google_data = response_json(session.get(GOOGLE_URL, headers=offer_headers, timeout=20))
        url_pattern = re.compile(r"https?://serviceactivation\.google\.com/subscription/new/[A-Za-z0-9_-]{50,}={0,2}")
        match = url_pattern.search(str(google_data.get("redirectionURL", "")))
        if not match: return "no_activation_url", ""

        try:
            human_delay(1.0, 2.0)
            session.get(SUBMIT_URL, headers=offer_headers, timeout=20)
        except: pass
        return "success", match.group(0)
    except: return "activation_api_failed", ""

# ============ دوال GrizzlySMS ============
async def cancel_active_grizzly_numbers(api_key):
    url = f"{GRIZZLY_API_URL}?api_key={api_key}&action=getActiveActivations"
    count = 0
    try:
        resp = await asyncio.to_thread(requests.get, url, timeout=15)
        data = resp.json()
        if data.get("status") == "success":
            for item in data.get("activeActivations", []):
                tzid = item.get("activationId")
                if tzid:
                    await cancel_grizzly_number(api_key, tzid)
                    count += 1
    except: pass
    return count

async def get_grizzly_number(api_key):
    url = f"{GRIZZLY_API_URL}?api_key={api_key}&action=getNumber&service=jio&country=22"
    try:
        resp = await asyncio.to_thread(requests.get, url, timeout=15)
        text = resp.text.strip()
        if text.startswith("ACCESS_NUMBER"):
            parts = text.split(":")
            return parts[1], parts[2]
    except: pass
    return None, None

async def check_grizzly_otp(api_key, tzid):
    url = f"{GRIZZLY_API_URL}?api_key={api_key}&action=getStatus&id={tzid}"
    try:
        resp = await asyncio.to_thread(requests.get, url, timeout=10)
        text = resp.text.strip()
        if text.startswith("STATUS_OK"):
            return text.split(":")[1]
    except: pass
    return None

async def cancel_grizzly_number(api_key, tzid):
    url = f"{GRIZZLY_API_URL}?api_key={api_key}&action=setStatus&status=8&id={tzid}"
    try: await asyncio.to_thread(requests.get, url, timeout=10)
    except: pass

# ============ المنظومة الذاتية للمستخدم ============
async def worker_task(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    session_data = active_sessions[user_id]
    api_key = session_data.api_key
    
    while session_data.status == "running":
        if session_data.active_workers >= 20:
            await asyncio.sleep(2)
            continue

        tzid, number = await get_grizzly_number(api_key)
        if not tzid:
            await asyncio.sleep(3)
            continue

        # تم شراء رقم بنجاح، نقوم بتحديث وقت آخر شراء
        session_data.last_buy_time = time.time()
        session_data.alert_sent = False 
        
        session_data.bought += 1
        session_data.active_workers += 1
        
        try:
            mobile = number[2:] if number.startswith("91") else number
            session = await asyncio.to_thread(create_jio_session)
            
            await async_human_delay(1.0, 2.5) 
            is_valid = await asyncio.to_thread(check_jio_number, session, mobile)
            if not is_valid:
                await cancel_grizzly_number(api_key, tzid)
                session_data.invalid_jio += 1
                continue

            await async_human_delay(1.5, 3.5)
            sent = await asyncio.to_thread(send_otp, session, mobile)
            if not sent:
                await cancel_grizzly_number(api_key, tzid)
                session_data.failed_send_otp += 1
                continue 

            start_time = time.time()
            otp_received = None
            while time.time() - start_time < 130:
                if session_data.status != "running" and session_data.status != "stopping": break
                otp = await check_grizzly_otp(api_key, tzid)
                if otp:
                    otp_received = otp.strip() 
                    break
                await asyncio.sleep(5)
                
            if not otp_received:
                await cancel_grizzly_number(api_key, tzid)
                session_data.cancelled_timeout += 1
                continue
                
            session_data.otp_received += 1
            await async_human_delay(3.5, 6.5)
            
            verified = await asyncio.to_thread(verify_otp, session, mobile, otp_received)
            if verified:
                await async_human_delay(2.0, 5.0)
                status, url = await asyncio.to_thread(extract_activation, session)
                if status == "success":
                    with open(f"gemini_links_{user_id}.txt", "a") as f: f.write(url + "\n")
                    session_data.success_links += 1
                    try:
                        # إرسال الرابط للمستخدم الخاص به فقط
                        await context.bot.send_message(chat_id=int(user_id), text=f"🎉 **تم صيد رابط جديد!**\n\n📱 الرقم: `{mobile}`\n🔗 الرابط:\n{url}", parse_mode='Markdown')
                    except: pass
                else: session_data.otp_rejected_jio += 1
            else: session_data.otp_rejected_jio += 1
        finally:
            session_data.active_workers -= 1

def get_dashboard_text(user_id):
    s = active_sessions[user_id]
    status_emoji = "🟢" if s.status == "running" else "🟡" if s.status == "stopping" else "🔴"
    status_text = "يعمل" if s.status == "running" else "جاري التصفية..." if s.status == "stopping" else "متوقف"
    
    return (
        f"📊 **إحصائيات حسابك المباشرة:**\n\n"
        f"الحالة: {status_emoji} {status_text}\n"
        f"⚙️ **الخطوط النشطة الآن:** `{s.active_workers} / 20`\n"
        f"🛒 **إجمالي المشتراة:** `{s.bought}`\n"
        f"✅ **الروابط المستخرجة:** `{s.success_links}`\n"
        f"⚠️ **تفاصيل الفشل:**\n"
        f"⏳ ملغاة لعدم وصول الكود: `{s.cancelled_timeout}`\n"
        f"❌ ليس Jio (مرفوض من البداية): `{s.invalid_jio}`\n"
        f"🔄 *يتم التحديث تلقائياً...*"
    )

async def update_dashboard(user_id, context: ContextTypes.DEFAULT_TYPE):
    s = active_sessions[user_id]
    while s.status in ["running", "stopping"]:
        # التحقق من مرور ساعة كاملة (3600 ثانية) بدون شراء رقم
        if s.status == "running" and (time.time() - s.last_buy_time > 3600) and not s.alert_sent:
            s.status = "stopping" # إيقاف تلقائي
            s.alert_sent = True
            try:
                await context.bot.send_message(chat_id=int(user_id), text="⚠️ **تنبيه هام:**\nمرت ساعة كاملة ولم أتمكن من سحب أي رقم!\n\nقد يكون رصيدك قد نفد في موقع الأرقام، أو لا يوجد أرقام متوفرة حالياً.\n*تم إيقاف السحب مؤقتاً لتجنب المشاكل.*", parse_mode='Markdown')
            except: pass

        if s.message_obj:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف النظام", callback_data='stop_bot')]]) if s.status == "running" else None
            try: await s.message_obj.edit_text(text=get_dashboard_text(user_id), reply_markup=markup, parse_mode='Markdown')
            except: pass
                
        if s.status == "stopping" and s.active_workers <= 0:
            s.status = "stopped"
            s.active_workers = 0 
            try: await s.message_obj.edit_text(text=get_dashboard_text(user_id).replace("🔄 *يتم التحديث تلقائياً...*", "🛑 **[تم الإيقاف]** الساحة نظيفة."), parse_mode='Markdown')
            except: pass
            break
        await asyncio.sleep(4)

# ============ واجهة المستخدم والأزرار ============
def user_main_menu():
    keyboard = [
        [InlineKeyboardButton("▶️ بدء السحب", callback_data='run_bot'),
         InlineKeyboardButton("🛑 إيقاف السحب", callback_data='stop_bot')],
        [InlineKeyboardButton("🗑 إلغاء الأرقام المعلقة", callback_data='cancel_pending')],
        [InlineKeyboardButton("⚙️ تغيير مفتاح API", callback_data='change_api'),
         InlineKeyboardButton("📊 عرض الإحصائيات", callback_data='show_stats')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    
    if uid not in users_db:
        users_db[uid] = {"username": user.username, "approved": False, "api_key": None, "banned": False}
        save_db(users_db)
        await context.bot.send_message(ADMIN_ID, f"🔔 مستخدم جديد يطلب الصلاحية:\nيوزر: @{user.username}\nأيدي: `{uid}`\nللموافقة: `/approve {uid}`", parse_mode='Markdown')
    
    if users_db[uid].get("banned"):
        await update.message.reply_text("❌ حسابك محظور من استخدام البوت.")
        return
        
    if not users_db[uid]["approved"]:
        await update.message.reply_text("⏳ حسابك قيد المراجعة. تم إرسال طلبك للإدارة.")
        return
        
    if not users_db[uid]["api_key"]:
        await update.message.reply_text("✅ حسابك مفعل! أرسل مفتاح موقع الأرقام (API Key) الخاص بك كرسالة نصية الآن.")
        return
        
    await update.message.reply_text(f"👋 أهلاً بك يا {user.first_name} في لوحة التحكم الخاصة بك:\n\nاختر من الأزرار أدناه للتحكم بحسابك:", reply_markup=user_main_menu())

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    
    if uid in users_db and users_db[uid]["approved"]:
        # إذا كان ينتظر وضع مفتاح
        users_db[uid]["api_key"] = text
        save_db(users_db)
        await update.message.reply_text("✅ تم حفظ مفتاح الـ API بنجاح! يمكنك الآن استخدام اللوحة.", reply_markup=user_main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = str(query.from_user.id)
    data = query.data
    
    if uid not in users_db or not users_db[uid].get("approved") or users_db[uid].get("banned"):
        await query.answer("❌ ليس لديك صلاحية.", show_alert=True)
        return

    if not users_db[uid].get("api_key"):
        await query.answer("⚠️ يجب إرسال مفتاح API أولاً.", show_alert=True)
        return

    api_key = users_db[uid]["api_key"]

    if uid not in active_sessions:
        active_sessions[uid] = UserSession(uid, api_key)
    session = active_sessions[uid]

    if data == 'run_bot':
        if session.status in ["running", "stopping"]:
            await query.answer("⚠️ نظامك يعمل بالفعل أو قيد الإيقاف!", show_alert=True)
            return

        await query.answer("🚀 جاري بدء النظام...", show_alert=False)
        msg = await query.message.reply_text("🧹 **جاري فحص وإلغاء الأرقام المعلقة بحسابك قبل البدء...**", parse_mode='Markdown')
        cancelled = await cancel_active_grizzly_numbers(api_key)
        await msg.edit_text(f"✅ **تم تنظيف {cancelled} أرقام معلقة. جارٍ إطلاق العمال...**", parse_mode='Markdown')
        
        session.__init__(uid, api_key) # تصفير الإحصائيات وبدء الوقت
        session.status = "running"
        
        session.message_obj = await query.message.reply_text(
            text=get_dashboard_text(uid),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف النظام", callback_data='stop_bot')]]),
            parse_mode='Markdown'
        )

        for i in range(20):
            await asyncio.sleep(random.uniform(1.0, 2.5))
            asyncio.create_task(worker_task(context, uid))
            
        asyncio.create_task(update_dashboard(uid, context))

    elif data == 'stop_bot':
        if session.status == "running":
            session.status = "stopping"
            await query.answer("🛑 تم إرسال أمر الإيقاف. سيتم الانتظار لانتهاء الأرقام المفتوحة.", show_alert=True)
        else:
            await query.answer("النظام متوقف بالفعل.", show_alert=True)

    elif data == 'cancel_pending':
        await query.answer("🗑 جاري مسح جميع الأرقام المعلقة...", show_alert=False)
        msg = await query.message.reply_text("⏳ جاري الاتصال بالموقع لإلغاء الأرقام المعلقة...")
        cancelled = await cancel_active_grizzly_numbers(api_key)
        await msg.edit_text(f"✅ **تم بنجاح إلغاء {cancelled} رقم معلق، وتم استرداد رصيدها!**", parse_mode='Markdown')

    elif data == 'change_api':
        users_db[uid]["api_key"] = None
        save_db(users_db)
        if session.status == "running":
            session.status = "stopping"
        await query.answer("تم مسح المفتاح الحالي.", show_alert=True)
        await query.message.reply_text("⚙️ أرسل مفتاح API الجديد الخاص بك كرسالة نصية الآن:")

    elif data == 'show_stats':
        if session.status in ["running", "stopping"]:
            await query.answer("انظر إلى الرسالة المحدثة في الأسفل 👇", show_alert=True)
        else:
            await query.message.reply_text(get_dashboard_text(uid), parse_mode='Markdown')


# ============ أوامر الإدارة ============
async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target_id = context.args[0]
        if target_id in users_db:
            users_db[target_id]["approved"] = True
            users_db[target_id]["banned"] = False
            save_db(users_db)
            await update.message.reply_text(f"✅ تم تفعيل المستخدم {target_id}")
            await context.bot.send_message(chat_id=target_id, text="🎉 تم قبولك من قبل الإدارة!\nاضغط /start للدخول للوحة التحكم.")
    except: await update.message.reply_text("❌ الاستخدام الصحيح: /approve <user_id>")

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target_id = context.args[0]
        if target_id in users_db:
            users_db[target_id]["banned"] = True
            users_db[target_id]["approved"] = False
            save_db(users_db)
            if target_id in active_sessions: active_sessions[target_id].status = "stopping"
            await update.message.reply_text(f"✅ تم حظر وإيقاف المستخدم {target_id}")
    except: await update.message.reply_text("❌ الاستخدام الصحيح: /ban <user_id>")

def main():
    if not BOT_TOKEN:
        logger.error("لم يتم العثور على توكن البوت! تأكد من إضافته في متغيرات Railway.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_bot))
    application.add_handler(CommandHandler("approve", admin_approve))
    application.add_handler(CommandHandler("ban", admin_ban))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(button_handler)) # يستقبل كل الأزرار
    
    application.run_polling()

if __name__ == "__main__":
    main()
