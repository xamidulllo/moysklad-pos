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

# Ilova endi shaxsiy MoySklad login/parol so'ramaydi (barcha kassirlar
# baribir bitta umumiy hisobdan foydalanar edi — shu sabab bu qadam faqat
# sekinlik va token to'qnashuvlarining manbai edi, xavfsizlik emas). Kirish
# ekrani endi faqat kassir ismini so'raydi. Ixtiyoriy umumiy PIN — agar
# ilova havolasi tashqariga chiqib qolsa ham, begona hech qanday MoySklad
# parolisiz kassaga kira olmasligi uchun oddiy qo'shimcha to'siq (yoqilmagan
# bo'lsa — bo'sh qoldirilsa — PIN so'ralmaydi).
SHOP_ACCESS_PIN = os.getenv("SHOP_ACCESS_PIN", "").strip() or None

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

# ---------------------------------------------------------------------------
# "Do'kon" rejimi — checkout doim BITTA tashkilot/loyiha/kontragentga yozadi
# (tanlash ekranlari yo'q), kunlik sotuvlar BITTA MoySklad zakazi+otgruzkasiga
# birlashtiriladi, har bir sotuv esa o'z to'lov hujjatini oladi (shop_sync.py'ga
# qarang). Sinov paytida SHOP_PROJECT_NAME/SHOP_AGENT_NAME'ni test loyiha/
# mijozga almashtirib qo'yish mumkin — haqiqiy "Do'kon"/"Do'kon kliyent"ga
# hech qachon sinov buyurtmasi kirmasligi kerak.
# ---------------------------------------------------------------------------
SHOP_ORGANIZATION_ID = os.getenv("SHOP_ORGANIZATION_ID", "").strip() or None
SHOP_PROJECT_NAME = os.getenv("SHOP_PROJECT_NAME", "Do'kon").strip()
# MUHIM (real hisobda tekshirilgan): mijozning HAQIQIY nomi "kilyent" deb
# yozilgan (odatdagi "kliyent" imlosi emas) — aniq shu yozilishda MoySklad'da
# mavjud, boshqacha yozilsa hech qanday mos kelmaydi.
SHOP_AGENT_NAME = os.getenv("SHOP_AGENT_NAME", "Do'kon kilyent").strip()

# Narx turi ustuvorlik tartibi — ro'yxatdagi birinchi topilgan nom ishlatiladi,
# hech biri topilmasa MoySklad'ning standart "Цена продажи"siga tushiladi.
# MUHIM (real hisobda tekshirilgan): haqiqiy nomi "Do'kon Sotov" (katta S,
# "sotuv" emas "sotov").
SHOP_PRICE_TYPE_NAMES = [
    name.strip()
    for name in os.getenv("SHOP_PRICE_TYPE_NAMES", "Do'kon Sotov").split(",")
    if name.strip()
]

# To'lov shoti tanlashda FAQAT shu nomlar ko'rsatiladi (aniq mos kelishi kerak).
SHOP_ALLOWED_ACCOUNT_NAMES = [
    name.strip()
    for name in os.getenv(
        "SHOP_ALLOWED_ACCOUNT_NAMES",
        "Donyor aka naxt so'm,Donyor aka karta,Donyor aka naxt,"
        "Do'kon naxt $,Do'kon naxt so'm,Do'kon ...7789 Mahkamov Fazliddina",
    ).split(",")
    if name.strip()
]

# Ish kuni chegarasi — "HH:MM" formatida, har kuni shu vaqtda eski kun yopilib,
# yangisi ochiladi (yarim tun emas).
SHOP_DAY_CUTOFF = os.getenv("SHOP_DAY_CUTOFF", "16:50").strip()

# Chet el valyutasida naqd to'lov + mahalliy valyutada qaytim oqimi uchun —
# qaytim shu (so'm) shotdan chiqim sifatida yoziladi.
SHOP_SOM_ACCOUNT_NAME = os.getenv("SHOP_SOM_ACCOUNT_NAME", "Do'kon naxt so'm").strip()
SHOP_CHANGE_EXPENSE_ARTICLE_NAME = os.getenv("SHOP_CHANGE_EXPENSE_ARTICLE_NAME", "Qaytim").strip()

# Standart dollar kursi (1 USD = ? so'm) — ilova ichida o'zgartirish mumkin.
DEFAULT_EXCHANGE_RATE = float(os.getenv("DEFAULT_EXCHANGE_RATE", "12000"))
