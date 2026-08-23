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
ADMIN_ID = 643309456 # الأيدي الخاص بك

# الأيديات المسموح لها بالدخول مباشرة (VIP)
ALLOWED_IDS = ["1715862764", "643309456", str(ADMIN_ID)]

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
        self.last_buy_time = time.time()
        self.alert_sent = False 
        self.target_limit = 0  # 0 يعني لا محدود
        self.max_workers = 20
        # إضافات التحكم بالمهام
        self.limit_lock = None  
        self.tasks_reserved = 0 

active_sessions = {}

# ============ دوال التأخير والتصفح والإيموجيات ============
def human_delay(min_sec=1.5, max_sec=3.5):
    time.sleep(random.uniform(min_sec, max_sec))

async def async_human_delay(min_sec=2.0, max_sec=4.5):
    await asyncio.sleep(random.uniform(min_sec, max_sec))

def add_custom_emojis(text: str) -> str:
    """استبدال أسماء الدول بإيموجيات مخصصة (Custom Emojis) بصيغة HTML"""
    replacements = {
        r"(الكويت|دينار كويتي)": r"\1 <tg-emoji emoji-id='5221949726718442491'>🇰🇼</tg-emoji>",
        r"(السعودية|ريال سعودي)": r"\1 <tg-emoji emoji-id='5224698145010624573'>🇸🇦</tg-emoji>",
        r"(الامارات|درهم اماراتي|درهم إماراتي|الاماراتب)": r"\1 <tg-emoji emoji-id='5224565851427976312'>🇦🇪</tg-emoji>",
        r"(السودان|جنيه سوداني)": r"\1 <tg-emoji emoji-id='5224372990216514135'>🇸🇩</tg-emoji>"
    }
    for pattern, emoji_tag in replacements.items():
        text = re.sub(pattern, emoji_tag, text)
    return text

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
                    success = await cancel_grizzly_number(api_key, tzid)
                    if success:
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

async def cancel_grizzly_number(api_key, tzid) -> bool:
    """دالة الإلغاء الدقيقة - تتأكد من رد السيرفر الفعلي"""
    url = f"{GRIZZLY_API_URL}?api_key={api_key}&action=setStatus&status=8&id={tzid}"
    try: 
        resp = await asyncio.to_thread(requests.get, url, timeout=10)
        text = resp.text.strip()
        if "ACCESS_CANCEL" in text:
            return True
        # إذا رفض السيرفر الإلغاء المبكر (EARLY_CANCEL_DENIED)، يعتبر الرقم غير ملغى حالياً
    except: pass
    return False

# ============ المنظومة الذاتية للمستخدم ============

async def reserve_task(session: UserSession) -> bool:
    """دالة حجز المهام لضمان عدم تجاوز العدد المطلوب"""
    async with session.limit_lock:
        if session.status != "running":
            return False
        if session.target_limit > 0 and session.tasks_reserved >= session.target_limit:
            return False
        session.tasks_reserved += 1
        return True

async def worker_task(context: ContextTypes.DEFAULT_TYPE, user_id: str):
    session_data = active_sessions[user_id]
    api_key = session_data.api_key
    
    while session_data.status == "running":
        
        # التأكد من عدم تجاوز عدد الـ workers المسموح
        if session_data.active_workers >= session_data.max_workers:
            await asyncio.sleep(2)
            continue

        # محاولة حجز مساحة للعملية (لضبط العدد المخصص)
        allowed = await reserve_task(session_data)
        if not allowed:
            break # إذا وصلنا للهدف، العامل يتوقف

        tzid, number = await get_grizzly_number(api_key)
        if not tzid:
            # إذا فشل شراء الرقم، نلغي الحجز حتى نعوضه برقم ثاني
            async with session_data.limit_lock:
                session_data.tasks_reserved -= 1
            await asyncio.sleep(3)
            continue

        session_data.last_buy_time = time.time()
        session_data.alert_sent = False 
        session_data.bought += 1
        session_data.active_workers += 1
        success_in_this_run = False
        
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
            # انتظار الكود لمدة دقيقتين و 10 ثواني (130 ثانية)
            while time.time() - start_time < 130:
                if session_data.status not in ["running", "stopping"]: break
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
                    success_in_this_run = True
                    with open(f"gemini_links_{user_id}.txt", "a") as f: f.write(url + "\n")
                    session_data.success_links += 1
                    
                    text_msg = f"🎉 <b>تم صيد رابط جديد!</b>\n\n📱 الرقم: <code>{mobile}</code>\n🔗 الرابط:\n{url}"
                    # تطبيق الإيموجيات على النص
                    text_msg = add_custom_emojis(text_msg)
                    try:
                        await context.bot.send_message(
                            chat_id=int(user_id), 
                            text=text_msg, 
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        logger.error(f"خطأ في إرسال الرابط: {e}")
                else: session_data.otp_rejected_jio += 1
            else: session_data.otp_rejected_jio += 1
        finally:
            session_data.active_workers -= 1
            # إذا لم تنجح العملية، نلغي حجز المهمة ليتم تعويضها برقم آخر
            if not success_in_this_run:
                async with session_data.limit_lock:
                    session_data.tasks_reserved -= 1

def get_dashboard_text(user_id):
    s = active_sessions[user_id]
    status_emoji = "🟢" if s.status == "running" else "🟡" if s.status == "stopping" else "🔴"
    status_text = "يعمل" if s.status == "running" else "جاري التصفية..." if s.status == "stopping" else "متوقف"
    
    target_str = f"{s.target_limit}" if s.target_limit > 0 else "غير محدود ♾️"
    
    # تحويل الواجهة لـ HTML لدعم Custom Emojis
    raw_text = (
        f"📊 <b>إحصائيات الحساب المباشرة:</b>\n\n"
        f"الحالة: {status_emoji} {status_text}\n"
        f"🎯 <b>الهدف المطلوب:</b> <code>{target_str}</code>\n"
        f"⚙️ <b>الخطوط النشطة الآن:</b> <code>{s.active_workers} / {s.max_workers}</code>\n"
        f"🛒 <b>إجمالي المشتراة:</b> <code>{s.bought}</code>\n"
        f"📩 <b>أكواد OTP المستلمة:</b> <code>{s.otp_received}</code>\n"
        f"✅ <b>الروابط المستخرجة:</b> <code>{s.success_links}</code>\n\n"
        f"⚠️ <b>تفاصيل الفشل:</b>\n"
        f"⏳ ملغاة لعدم وصول الكود: <code>{s.cancelled_timeout}</code>\n"
        f"❌ ليس Jio (مرفوض من البداية): <code>{s.invalid_jio}</code>\n"
        f"🔄 <i>يتم التحديث تلقائياً...</i>"
    )
    return add_custom_emojis(raw_text)

async def update_dashboard(user_id, context: ContextTypes.DEFAULT_TYPE):
    s = active_sessions[user_id]
    while s.status in ["running", "stopping"]:
        # التوقف التلقائي عند اكتمال الهدف الحقيقي (الروابط المستخرجة)
        if s.status == "running" and s.target_limit > 0 and s.success_links >= s.target_limit:
            s.status = "stopping"
            try:
                await context.bot.send_message(
                    chat_id=int(user_id), 
                    text=add_custom_emojis(f"🎯 <b>اكتمل الهدف بنجاح!</b>\nتم استخراج {s.target_limit} روابط، وتم إيقاف النظام تلقائياً لإيقاف صرف الرصيد."), 
                    parse_mode='HTML'
                )
            except: pass

        # التنبيه في حال مرور ساعة بلا رصيد
        if s.status == "running" and (time.time() - s.last_buy_time > 3600) and not s.alert_sent:
            s.status = "stopping" 
            s.alert_sent = True
            try:
                await context.bot.send_message(chat_id=int(user_id), text="⚠️ <b>تنبيه هام:</b>\nمرت ساعة كاملة ولم أتمكن من سحب أي رقم!\nتم إيقاف السحب مؤقتاً لتجنب المشاكل.", parse_mode='HTML')
            except: pass

        if s.message_obj:
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف النظام", callback_data='stop_bot')]]) if s.status == "running" else None
            try: await s.message_obj.edit_text(text=get_dashboard_text(user_id), reply_markup=markup, parse_mode='HTML')
            except: pass
                
        if s.status == "stopping" and s.active_workers <= 0:
            s.status = "stopped"
            s.active_workers = 0 
            try: await s.message_obj.edit_text(text=get_dashboard_text(user_id).replace("🔄 <i>يتم التحديث تلقائياً...</i>", "🛑 <b>[تم الإيقاف]</b> الساحة نظيفة."), parse_mode='HTML')
            except: pass
            break
        await asyncio.sleep(4)

async def start_extraction(uid, api_key, context, target_limit, workers_count, message_obj):
    if uid not in active_sessions:
        active_sessions[uid] = UserSession(uid, api_key)
    session = active_sessions[uid]
    
    msg = await message_obj.reply_text("🧹 <b>جاري الاتصال بالموقع وإلغاء أي أرقام معلقة بحسابك قبل البدء...</b>", parse_mode='HTML')
    cancelled = await cancel_active_grizzly_numbers(api_key)
    
    mode_text = f"عدد محدد ({target_limit})" if target_limit > 0 else "شراء مفتوح (حسب الرصيد)"
    await msg.edit_text(f"✅ <b>تم تنظيف {cancelled} أرقام معلقة.</b>\n🚀 <b>جاري بدء التشغيل...</b>\nالوضع: <code>{mode_text}</code>", parse_mode='HTML')
    
    session.__init__(uid, api_key) 
    session.target_limit = target_limit
    session.max_workers = workers_count
    session.status = "running"
    session.limit_lock = asyncio.Lock()  # تهيئة القفل
    session.tasks_reserved = 0
    
    session.message_obj = await message_obj.reply_text(
        text=get_dashboard_text(uid),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 إيقاف النظام", callback_data='stop_bot')]]),
        parse_mode='HTML'
    )

    for i in range(workers_count):
        await asyncio.sleep(random.uniform(1.0, 2.5))
        asyncio.create_task(worker_task(context, uid))
        
    asyncio.create_task(update_dashboard(uid, context))


# ============ واجهة المستخدم والأزرار ============
def user_main_menu(user_id):
    keyboard = [
        [InlineKeyboardButton("▶️ بدء السحب", callback_data='menu_run_options')],
        [InlineKeyboardButton("🗑 إلغاء الأرقام المعلقة", callback_data='cancel_pending')],
        [InlineKeyboardButton("⚙️ تغيير مفتاح API", callback_data='change_api'),
         InlineKeyboardButton("📊 عرض الإحصائيات", callback_data='show_stats')]
    ]
    if int(user_id) == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👨‍💻 لوحة الإدارة", callback_data='admin_panel')])
    return InlineKeyboardMarkup(keyboard)

def run_options_menu():
    keyboard = [
        [InlineKeyboardButton("1️⃣ استخراج 1 رابط (تلقائي مع التعويض)", callback_data='run_mode_1')],
        [InlineKeyboardButton("🔢 إدخال عدد مخصص من الروابط", callback_data='run_mode_custom')],
        [InlineKeyboardButton("♾️ سحب مستمر (شراء بلا حدود)", callback_data='run_mode_unlimited')],
        [InlineKeyboardButton("🔙 رجوع للرئيسية", callback_data='main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_main_menu():
    keyboard = [
        [InlineKeyboardButton("👥 إحصائيات المستخدمين النشطين", callback_data='admin_users_stats')],
        [InlineKeyboardButton("🔗 جلب ملفات الروابط للمستخدمين", callback_data='admin_get_links')],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data='main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    
    if uid not in users_db:
        # نظام الصلاحيات الفورية للـ VIP
        is_approved = uid in ALLOWED_IDS
        users_db[uid] = {"username": user.username, "approved": is_approved, "api_key": None, "banned": False, "state": "idle"}
        save_db(users_db)
        if not is_approved:
            await context.bot.send_message(ADMIN_ID, f"🔔 مستخدم جديد يطلب الصلاحية:\nيوزر: @{user.username}\nأيدي: <code>{uid}</code>\nللموافقة: /approve {uid}", parse_mode='HTML')
    
    if users_db[uid].get("banned"):
        await update.message.reply_text("❌ حسابك محظور من استخدام البوت.")
        return
        
    if not users_db[uid]["approved"]:
        await update.message.reply_text("⏳ حسابك قيد المراجعة. تم إرسال طلبك للإدارة.")
        return
        
    if not users_db[uid]["api_key"]:
        await update.message.reply_text("✅ حسابك مفعل! أرسل مفتاح موقع الأرقام (API Key) الخاص بك كرسالة نصية الآن.")
        return
        
    users_db[uid]["state"] = "idle"
    save_db(users_db)
    await update.message.reply_text(f"👋 أهلاً بك في لوحة التحكم الخاصة بك:\n\nاختر من الأزرار أدناه للتحكم بحسابك:", reply_markup=user_main_menu(uid))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text.strip()
    
    if uid not in users_db or not users_db[uid].get("approved"):
        return

    # إذا كان المستخدم ينتظر إدخال عدد الروابط المخصص
    if users_db[uid].get("state") == "waiting_limit":
        if text.isdigit() and int(text) > 0:
            limit = int(text)
            users_db[uid]["state"] = "idle"
            save_db(users_db)
            workers = min(limit, 20) # لا يتجاوز 20 خط بالوقت الواحد
            await update.message.reply_text(f"✅ تم استلام الهدف: {limit} رابط.")
            await start_extraction(uid, users_db[uid]["api_key"], context, limit, workers, update.message)
        else:
            await update.message.reply_text("❌ يرجى إرسال رقم صحيح أكبر من الصفر (مثال: 5).")
        return

    # إذا كان المستخدم ينتظر إدخال مفتاح الـ API
    if not users_db[uid].get("api_key"):
        users_db[uid]["api_key"] = text
        users_db[uid]["state"] = "idle"
        save_db(users_db)
        await update.message.reply_text("✅ تم حفظ مفتاح الـ API بنجاح! يمكنك الآن استخدام اللوحة.", reply_markup=user_main_menu(uid))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = str(query.from_user.id)
    data = query.data
    
    if uid not in users_db or not users_db[uid].get("approved") or users_db[uid].get("banned"):
        await query.answer("❌ ليس لديك صلاحية.", show_alert=True)
        return

    # إرجاع الحالة للطبيعي بمجرد الضغط على أي زر
    if users_db[uid].get("state") != "idle":
        users_db[uid]["state"] = "idle"
        save_db(users_db)

    if data == 'main_menu':
        await query.message.edit_text("👋 أهلاً بك في لوحة التحكم الخاصة بك:\n\nاختر من الأزرار أدناه للتحكم بحسابك:", reply_markup=user_main_menu(uid))
        return

    # ============== أزرار الإدارة ==============
    if data == 'admin_panel':
        if int(uid) != ADMIN_ID: return
        await query.message.edit_text("👨‍💻 <b>لوحة تحكم الإدارة:</b>\nاختر الإجراء المطلوب:", reply_markup=admin_main_menu(), parse_mode='HTML')
        return

    if data == 'admin_users_stats':
        if int(uid) != ADMIN_ID: return
        keyboard = []
        for u_id, u_data in users_db.items():
            if u_data.get("approved"):
                username = u_data.get('username') or u_id
                keyboard.append([InlineKeyboardButton(f"👤 عرض إحصائيات: {username}", callback_data=f'view_u_{u_id}')])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data='admin_panel')])
        
        await query.message.edit_text("👥 <b>اختر المستخدم لعرض إحصائياته الحالية:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if data.startswith('view_u_'):
        if int(uid) != ADMIN_ID: return
        target_uid = data.split('view_u_')[1]
        
        if target_uid in active_sessions:
            text = get_dashboard_text(target_uid)
        else:
            text = f"⚠️ المستخدم متوقف عن العمل حالياً أو لم يقم بتشغيل السحب."
            
        keyboard = [[InlineKeyboardButton("🔙 رجوع لقائمة المستخدمين", callback_data='admin_users_stats')]]
        await query.message.edit_text(f"👤 <b>إحصائيات المستخدم <code>{target_uid}</code>:</b>\n\n{text}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return

    if data == 'admin_get_links':
        if int(uid) != ADMIN_ID: return
        await query.message.reply_text("📂 جاري البحث عن ملفات الروابط للمستخدمين...")
        files_found = False
        for file in os.listdir():
            if file.startswith("gemini_links_") and file.endswith(".txt"):
                files_found = True
                target_uid = file.split('_')[2].split('.')[0]
                username = users_db.get(target_uid, {}).get("username", "مجهول")
                with open(file, 'rb') as doc:
                    await context.bot.send_document(chat_id=ADMIN_ID, document=doc, caption=f"🔗 روابط المستخدم: @{username}\nأيدي: <code>{target_uid}</code>", parse_mode='HTML')
        
        if not files_found:
            await query.message.reply_text("⚠️ لا توجد أي ملفات روابط مسجلة حتى الآن.")
        return
    # ============================================

    if not users_db[uid].get("api_key"):
        await query.answer("⚠️ يجب إرسال مفتاح API أولاً.", show_alert=True)
        return

    api_key = users_db[uid]["api_key"]
    session = active_sessions.get(uid)

    if data == 'menu_run_options':
        if session and session.status in ["running", "stopping"]:
            await query.answer("⚠️ نظامك يعمل بالفعل أو قيد الإيقاف!", show_alert=True)
            return
        await query.message.edit_text("⚙️ <b>إعدادات بدء السحب:</b>\n\nاختر وضع التشغيل المناسب لك:", reply_markup=run_options_menu(), parse_mode='HTML')

    elif data == 'run_mode_1':
        await query.answer("🚀 بدء سحب رابط واحد...", show_alert=False)
        # هدف 1، ويعمل بخط واحد (إذا فشل الرقم ينتظر ويعوضه تلقائياً حتى ينجح ثم يتوقف)
        await start_extraction(uid, api_key, context, target_limit=1, workers_count=1, message_obj=query.message)

    elif data == 'run_mode_custom':
        users_db[uid]["state"] = "waiting_limit"
        save_db(users_db)
        await query.message.edit_text("🔢 <b>أرسل الآن بالدردشة عدد الروابط التي تريد استخراجها:</b>\n\n<i>(اكتب رقماً فقط، مثال: 5 وسيتم توقيف البوت تلقائياً عند صيد 5 روابط)</i>", parse_mode='HTML')

    elif data == 'run_mode_unlimited':
        await query.answer("⚠️ تنبيه: هذا الوضع يعمل تلقائياً وسيتم السحب من الرصيد بشكل مستمر حتى تقوم بإيقافه يدوياً!", show_alert=True)
        # هدف 0 (مفتوح)، ويعمل بـ 20 خط
        await start_extraction(uid, api_key, context, target_limit=0, workers_count=20, message_obj=query.message)

    elif data == 'stop_bot':
        if session and session.status == "running":
            session.status = "stopping"
            await query.answer("🛑 تم إرسال أمر الإيقاف. سيتم الانتظار لانتهاء الأرقام المفتوحة.", show_alert=True)
        else:
            await query.answer("النظام متوقف بالفعل.", show_alert=True)

    elif data == 'cancel_pending':
        await query.answer("🗑 جاري مسح جميع الأرقام المعلقة...", show_alert=False)
        msg = await query.message.reply_text("⏳ جاري الاتصال بالموقع لإلغاء الأرقام المعلقة...")
        cancelled = await cancel_active_grizzly_numbers(api_key)
        await msg.edit_text(f"✅ <b>تم بنجاح إلغاء {cancelled} رقم معلق، وتم استرداد رصيدها!</b>", parse_mode='HTML')

    elif data == 'change_api':
        users_db[uid]["api_key"] = None
        save_db(users_db)
        if session and session.status == "running":
            session.status = "stopping"
        await query.answer("تم مسح المفتاح الحالي.", show_alert=True)
        await query.message.reply_text("⚙️ أرسل مفتاح API الجديد الخاص بك كرسالة نصية الآن:")

    elif data == 'show_stats':
        if session and session.status in ["running", "stopping"]:
            await query.answer("انظر إلى الرسالة المحدثة في الأسفل 👇", show_alert=True)
        elif session:
            await query.message.reply_text(get_dashboard_text(uid), parse_mode='HTML')
        else:
            await query.answer("لا توجد إحصائيات لعرضها حالياً. اضغط بدء السحب أولاً.", show_alert=True)

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
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()

if __name__ == "__main__":
    main()
