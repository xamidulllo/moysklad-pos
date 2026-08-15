# MoySklad Mobil POS Kassa

MoySklad API bilan integratsiyalashgan mobil kassa (Point of Sale) veb-ilovasi.
Backend — FastAPI (Python), frontend — Vanilla JS PWA, mobil qurilmalar uchun optimallashtirilgan.
Har bir kassir o'zining shaxsiy MoySklad login/paroli bilan kiradi.

## Loyiha tuzilishi

```
moysklad-pos/
├── backend/
│   ├── app/
│   │   ├── main.py            # API marshrutlar (routes)
│   │   ├── auth.py            # Kassir sessiyalari (login/logout)
│   │   ├── moysklad_client.py # MoySklad'ga HTTP so'rovlar + login->token almashtirish
│   │   ├── schemas.py         # Pydantic modellar
│   │   └── config.py          # .env dan sozlamalarni o'qish
│   ├── requirements.txt
│   └── .env.example
└── frontend/
    ├── index.html              # Kirish ekrani + asosiy ilova
    ├── style.css
    ├── app.js
    ├── manifest.json          # PWA manifest
    └── sw.js                  # Service worker (offline shell)
```

## O'rnatish

1. Python 3.10+ o'rnatilgan bo'lishi kerak.
2. Virtual muhit yaratish va kutubxonalarni o'rnatish:

   ```powershell
   cd backend
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. `.env` faylini yarating (ixtiyoriy — standart qiymatlar ham ishlaydi):

   ```powershell
   Copy-Item .env.example .env
   ```

   **Diqqat:** endi bu yerga MoySklad tokeni yozilmaydi. Har bir kassir ilova
   ochilgandagi **kirish ekranida** o'zining shaxsiy MoySklad login/parolini
   kiritadi — xuddi `online.moysklad.ru`ga kirgandagidek.

## Ishga tushirish

```powershell
cd backend
venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Brauzerda oching: `http://localhost:8000` — kirish ekrani chiqadi, u yerga
MoySklad login/parolingizni kiritasiz.

Backend frontend papkasini avtomatik xizmat ko'rsatadi (bitta origin — CORS
muammolari yo'q, PWA to'g'ri ishlaydi). Mobil qurilmadan sinash uchun
kompyuteringiz IP manzilini ishlatib, `http://<kompyuter-ip>:8000` ni oching
(kompyuter va telefon bir xil Wi-Fi tarmog'ida bo'lishi kerak).

## Kirish tizimi qanday ishlaydi

1. Kassir kirish ekranida login/parolini kiritadi.
2. Backend buni MoySklad'ning o'z `POST /security/token` xizmati orqali haqiqiy
   MoySklad tokeniga almashtiradi (haqiqiy hisobda tekshirilgan).
3. Token backend xotirasida, tasodifiy sessiya ID'siga bog'lab saqlanadi;
   brauzerga faqat shu tasodifiy ID **httpOnly cookie** sifatida jo'natiladi —
   haqiqiy MoySklad tokeni brauzerga hech qachon chiqmaydi.
4. Shu sessiyadagi barcha MoySklad so'rovlari **shu aniq kassirning** nomidan
   yuboriladi — MoySklad tarixida qaysi xodim qaysi sotuvni qilgani ko'rinadi.
5. Header'dagi ⏻ tugmasi orqali chiqish (`/api/logout`) — sessiya serverda ham,
   cookie ham o'chiriladi.

**MUHIM CHEKLOV (haqiqiy hisobda tekshirilgan):** MoySklad bir login uchun
bir vaqtning o'zida faqat **bitta** token'ni faol saqlaydi. Agar bir xil
login/parol bilan ikkinchi qurilmadan/oynadan qayta kirilsa, birinchi
sessiyaning tokeni jim ravishda bekor bo'ladi va birinchi qurilma keyingi
so'rovda "sessiya tugadi" xatosini ko'rib, kirish ekraniga qaytariladi. Agar
bir nechta kassir **bir vaqtda** ishlashi kerak bo'lsa, ularning **har biriga
alohida MoySklad xodim (employee) hisobi** kerak — bitta umumiy login bir
nechta faol qurilma uchun mos emas.

- Sessiyalar backend jarayon xotirasida saqlanadi (standart: 12 soat, `.env`dagi
  `SESSION_TTL_HOURS` bilan o'zgartiriladi) — server qayta ishga tushirilsa,
  barcha kassirlar qaytadan kirishi kerak bo'ladi.
- Productionda HTTPS orqali ishga tushirilsa, `.env`da `SESSION_COOKIE_SECURE=true`
  qiling.

## Ishlash tartibi (workflow)

1. **Mahsulotlar** ekranida nom yoki SKU bo'yicha qidiriladi (`entity/assortment`),
   "+" tugmasi bilan savatga qo'shiladi.
2. **Savat** ekranida miqdor `+`/`−` orqali o'zgartiriladi, umumiy summa avtomatik hisoblanadi.
3. **To'lov** ekranida:
   - Tashkilot va ombor (agar bittadan ko'p bo'lsa, tanlash mumkin; bitta bo'lsa avtomatik tanlanadi).
   - Mijoz nomi bo'yicha qidiriladi (`entity/counterparty`).
   - To'lov hisobi ro'yxati **dinamik** ravishda `entity/organization/{id}/accounts`'dan
     yuklanadi — kod ichida hech qanday hisob ID si yozilmagan (pastdagi eslatmaga qarang).
   - **Hujjat turi** (Naqd/Bank) hisob nomiga qarab taxminiy tanlanadi, kassir kerak bo'lsa o'zgartiradi.
   - **Valyuta kursi** maydoniga kassir joriy kursni qo'lda kiritadi (faqat tashkilotning
     bazaviy valyutasidan farqli hisoblar uchun qo'llaniladi — pastga qarang).
4. **"To'lash"** tugmasi bosilganda backend uchta hujjatni ketma-ket, bir-biriga
   bog'lab yaratadi:

   ```
   Заказ покупателя (customerorder)
        │  savatdagi mahsulot/miqdorlar bilan
        ├──► Otgruzka/demand   ("customerOrder" maydoni orqali buyurtmaga bog'lanadi)
        └──► To'lov (cashin/paymentin)
              - "organizationAccount" orqali tanlangan hisobga,
              - "operations[].linkedSum" orqali BUYURTMAning o'ziga bog'lanadi,
              - "rate.value" orqali kassir kiritgan kursga ega bo'ladi
                (faqat chet el valyutasidagi hisoblarda).
   ```

   Agar zanjirning istalgan bosqichida xatolik chiqsa (masalan noto'g'ri hisob),
   shu paytgacha yaratilgan hujjatlar **avtomatik o'chiriladi** — omborda
   "osilib qolgan" to'lanmagan buyurtma/otgruzka qolmaydi.
   - Chek printeri yoki fiskal qurilma bilan **hech qanday integratsiya yo'q** —
     talabga ko'ra faqat MoySklad hujjatlari yaratiladi.

### MUHIM: Haqiqiy MoySklad API'da tekshirilgan cheklovlar

Loyiha haqiqiy MoySklad hisobiga ulanib, har bir bosqichda real so'rovlar bilan
sinovdan o'tkazildi. Shu jarayonda dastlabki taxminlardan farq qiladigan bir
nechta muhim xususiyat aniqlandi:

1. **Alohida "kassa" (cashaccount) entity'si mavjud emas.** MoySklad'da naqd va bank
   hisoblari bitta `entity/organization/{id}/accounts` to'plamida saqlanadi. Backend
   hisob nomiga qarab uni "naqd" yoki "bank" deb **taxmin qiladi** (`kassa`, `нал`,
   `naqd`, `cash` kabi kalit so'zlar bo'yicha) — bu aniq MoySklad maydoni emas, shuning
   uchun kassir hujjat turini to'lov ekranida tasdiqlashi yoki o'zgartirishi kerak.
2. **`cashin` (ПКО) hujjati muayyan hisobga bog'lanmaydi.** MoySklad faqat `paymentin`
   (Входящий платёж) uchun `organizationAccount` bog'lanishini haqiqatda saqlaydi;
   `cashin` orqali yuborilgan hisob havolasi e'tiborga olinmaydi (xato bermaydi, lekin
   MoySklad'da ko'rinmaydi) — ПКО faqat tashkilotning umumiy kassa balansini oshiradi.
   Agar sizga hisobga aniq bog'langan to'lov muhim bo'lsa, **"Bank"** turini tanlang.
3. **Valyuta kursi faqat chet el valyutasidagi hisoblar uchun ishlaydi.** MoySklad
   tashkilotning bazaviy (учетная) valyutasi uchun `rate.value != 1` yuborilsa xato
   3007 bilan rad etadi. Backend buni avtomatik tekshiradi: agar tanlangan hisob
   bazaviy valyutada bo'lsa, kassir kiritgan kurs jim ravishda e'tiborga olinmaydi.
   Chet el valyutasidagi hisob (masalan bank hisobi) tanlanganda kurs to'liq
   ishlaydi — bu haqiqiy hisobda ikkala holat uchun ham tekshirilgan.
4. **Login/parol → token almashtirish real ishlaydi**, lekin bir login uchun bir
   vaqtda faqat bitta token faol bo'ladi (yuqoridagi "Kirish tizimi" bo'limiga qarang).

## Xatoliklarni qayta ishlash

- Har bir API chaqiruvi `try/catch` bilan o'ralgan; MoySklad 400/401/500 xato qaytarsa,
  ekran pastida qizil toast-xabar orqali aniq sabab ko'rsatiladi
  (`{"errors":[{"error": "..."}]}` formatidagi MoySklad xabari o'qiladi).
- 401 (sessiya tugagan/yaroqsiz) kelsa, ilova avtomatik kirish ekraniga qaytaradi.
- Tarmoq yoki server xatosida ilova qulamaydi — foydalanuvchi qayta urinishi mumkin.

## Eslatmalar

- PWA ikonkasi sifatida vaqtinchalik `frontend/icon.svg` ishlatilmoqda (Android/Chrome
  uchun to'liq ishlaydi). iOS'da eng sifatli ko'rinish uchun productionda uni
  o'zingizning logotipingiz asosidagi haqiqiy `icon-192.png`/`icon-512.png`
  fayllariga almashtiring va `manifest.json` + `index.html`dagi havolalarni
  shunga mos yangilang.
- Ishlab chiqarish (production) muhitida `CORSMiddleware`dagi `allow_origins=["*"]`
  ni haqiqiy domeningiz bilan cheklash tavsiya etiladi.
- Kesh (`_cache` in `main.py`) hisoblar/tashkilotlar/omborlar uchun 60 soniyalik
  TTL bilan ishlaydi va har bir kassirning tokeni bo'yicha alohida saqlanadi
  (turli kassirlar bir-birining ma'lumotini ko'rmaydi).
- Sessiyalar jarayon xotirasida saqlanadi — bir nechta backend nusxasi
  (masalan load balancer ortida) ishlatiladigan production muhitda buni Redis
  kabi umumiy xotiraga ko'chirish kerak bo'ladi.
