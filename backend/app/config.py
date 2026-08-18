"""Ilova sozlamalari.

Endi bitta global MoySklad tokeni talab qilinmaydi — har bir kassir o'z shaxsiy
MoySklad login/paroli bilan kiradi (/api/login), backend buni MoySklad'ning o'z
POST /security/token xizmati orqali tokenga almashtiradi va vaqtinchalik
server-side sessiya sifatida saqlaydi (auth.py'ga qarang).
"""
import os
from dotenv import load_dotenv

load_dotenv()

MOYSKLAD_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

SESSION_COOKIE_NAME = "pos_session"
SESSION_TTL_HOURS = float(os.getenv("SESSION_TTL_HOURS", "12"))
# Productionda HTTPS orqali ishga tushirilganda .env'da SESSION_COOKIE_SECURE=true qiling.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"

# ---------------------------------------------------------------------------
# Google Sheets orqali buyurtmalarni navbatga qo'yish va MoySklad'ga davriy
# sinxronlash (ixtiyoriy funksiya — quyidagi o'zgaruvchilar sozlanmagan bo'lsa,
# checkout hozirgidek to'g'ridan-to'g'ri MoySklad'ga yozadi).
# ---------------------------------------------------------------------------

# "direct" — checkout darhol MoySklad'ga yozadi (hozirgi/standart xatti-harakat).
# "queue" — checkout faqat EXPECTED_MS_ORGANIZATION_ID'ga mos MoySklad hisobi
# uchun Google Sheets navbatiga yoziladi; boshqa har qanday login uchun baribir
# to'g'ridan-to'g'ri yoziladi (bu funksiya faqat bitta tashkilot uchun sozlangan).
CHECKOUT_MODE = os.getenv("CHECKOUT_MODE", "direct").strip().lower()

# Navbatga qo'yish faqat aynan shu MoySklad tashkilotining login'lari uchun
# ishlaydi (entity/organization href'idagi UUID) — boshqa hisoblar bilan
# aralashib ketmasligi uchun.
EXPECTED_MS_ORGANIZATION_ID = os.getenv("EXPECTED_MS_ORGANIZATION_ID", "").strip() or None

# Sinxronlash vazifasi MoySklad'ga navbatdagi buyurtmalarni yuborish uchun
# ishlatadigan login/parol — barcha kassirlar bitta umumiy MoySklad hisobidan
# foydalangani uchun, bu odatda ular ishlatadigan hisobning o'zi.
MS_SYNC_LOGIN = os.getenv("MS_SYNC_LOGIN", "").strip() or None
MS_SYNC_PASSWORD = os.getenv("MS_SYNC_PASSWORD", "").strip() or None

# Google Apps Script trigger shu maxfiy qiymatni "X-Sync-Secret" header'ida
# yuborishi kerak — bo'lmasa /api/sync/run so'rovni rad etadi.
SYNC_TRIGGER_SECRET = os.getenv("SYNC_TRIGGER_SECRET", "").strip() or None

# Google Sheets'ga ulanishning IKKI usuli qo'llab-quvvatlanadi — sheets_client.py
# avval OAuth (shaxsiy hisob) ma'lumotlari to'liq bo'lsa o'shani ishlatadi,
# aks holda service-account JSON kalitiga qaytadi.
#
# 1) OAuth (tavsiya — ko'p tashkilotlarda "Organization Policy" service-account
#    kalit yaratishni butunlay bloklab qo'yadi, "Secure by Default" siyosati
#    tufayli). Bir martalik login orqali olingan refresh_token — token muddati
#    tugasa ham, backend uni avtomatik yangilab turadi.
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip() or None
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip() or None
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip() or None

# 2) Service-account JSON kaliti, base64'da (Render kabi veb-panellarda ko'p
#    qatorli JSON'ni to'g'ridan-to'g'ri joylashtirish "private_key"dagi "\n"
#    belgilarini buzib qo'yishi ma'lum muammo — base64 buni butunlay oldini oladi).
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "").strip() or None

GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip() or None
GOOGLE_SHEETS_WORKSHEET_NAME = os.getenv("GOOGLE_SHEETS_WORKSHEET_NAME", "Orders").strip()
