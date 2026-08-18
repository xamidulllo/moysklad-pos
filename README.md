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

1. **Mahsulotlar** ekranida avval **ombor** tanlanadi (kirishdan so'ng darhol
   yuklanadi, bitta bo'lsa avtomatik tanlanadi), so'ng nom yoki SKU bo'yicha
   qidiriladi (`entity/assortment`). Har bir tovar qatorida shu TANLANGAN
   ombor bo'yicha aniq qoldiq ko'rsatiladi ("Omborda: N dona" / "Omborda
   yo'q") — `entity/assortment`ga `stockStore=<ombor href>` berilganda
   MoySklad har bir tovarni aynan shu ombordagi qoldig'i bilan qaytaradi
   (real API'da tekshirilgan). Ombor almashtirilsa, qidiruv natijasi ham
   avtomatik qayta yuklanadi. "+" tugmasi bilan savatga qo'shiladi.
2. **Savat** ekranida miqdor `+`/`−` orqali o'zgartiriladi, umumiy summa avtomatik hisoblanadi.
3. **To'lov** ekranida:
   - Tashkilot tanlanadi (ombor allaqachon tanlangan — bu yerda faqat
     ma'lumot sifatida ko'rsatiladi).
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
5. **Tarix** ekranida shu ilova orqali kiritilgan barcha buyurtmalar ko'rinadi
   (sana, mijoz, summa, to'langan/to'lanmagan holati, izoh). Har bir buyurtma
   yaratilganda unga avtomatik ravishda maxsus **"POS Mini App"** sotuv kanali
   (`entity/saleschannel`) biriktiriladi — shu orqali MoySklad'dagi boshqa
   botlar yoki qo'lda kiritilgan buyurtmalardan aniq ajratiladi
   (`GET /api/orders/history`, `filter=salesChannel=...` bo'yicha).

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
5. **Manfiy/nol qoldiqqa otgruzka taqiqi hisob sozlamasiga bog'liq**
   (`checkShippingStock`, MoySklad'da "Настройки" → "Общие"). Yoqilgan bo'lsa,
   `entity/demand` yaratishda MoySklad xato 3007 bilan rad etadi — xuddi
   sotuvchi MoySklad'ning o'z sahifasida ko'radigan xatoning aynan o'zi (real
   hisobda ushbu sozlamani vaqtincha yoqib, sinovdan o'tkazilgan). Backend
   buni alohida "tushunarli" xabarga aylantirmaydi — MoySklad'ning o'z xato
   matnini frontend'ga uzatadi va (agar otgruzka bosqichida yuz bersa) allaqachon
   yaratilgan buyurtmani avtomatik o'chiradi. Shu sabab **Mahsulotlar** ekranida
   qoldiq oldindan ko'rsatiladi (yuqoriga qarang) — kassir bu xatoga umuman
   duch kelmasdan oldin tovar yo'qligini bilib oladi.

## Xatoliklarni qayta ishlash

- Har bir API chaqiruvi `try/catch` bilan o'ralgan; MoySklad 400/401/500 xato qaytarsa,
  ekran pastida qizil toast-xabar orqali aniq sabab ko'rsatiladi
  (`{"errors":[{"error": "..."}]}` formatidagi MoySklad xabari o'qiladi).
- 401 (sessiya tugagan/yaroqsiz) kelsa, ilova avtomatik kirish ekraniga qaytaradi.
- Tarmoq yoki server xatosida ilova qulamaydi — foydalanuvchi qayta urinishi mumkin.

## Google Sheets navbati va davriy MoySklad sinxronlash (ixtiyoriy)

Standart holatda checkout hozirgidek ishlaydi: "To'lash" bosilganda buyurtma
**darhol** MoySklad'ga yoziladi (yuqoridagi "Ishlash tartibi"ga qarang).

Bundan tashqari, checkout **ikkinchi rejimda** ham ishlashi mumkin: buyurtma
MoySklad'ga tegmasdan avval Google Sheets'dagi bitta jadvalga ("navbat")
qator sifatida yoziladi, so'ng Google Apps Script'ning davriy trigger'i
(kuniga 4 marta: 00:00, 06:00, 12:00, 18:00, Asia/Tashkent) backend'ning
`/api/sync/run` marshrutini chaqiradi — shu marshrut navbatdagi barcha
buyurtmalarni MoySklad'ga bittalab, xuddi to'g'ridan-to'g'ri checkout'dagi
kabi (customerorder → demand → to'lov) ko'chiradi.

**Bu rejim faqat bitta MoySklad tashkiloti uchun yoqiladi** (quyidagi
`EXPECTED_MS_ORGANIZATION_ID`) — boshqa har qanday login bilan kirgan
kassirlar (masalan test hisobi) baribir to'g'ridan-to'g'ri, real vaqtda
checkout qilishda davom etadi.

### Nega kerak bo'lishi mumkin, va qanday xavfsiz qilingan

Buyurtmani darhol emas, keyinroq (kuniga 4 marta) MoySklad'ga yozish — eng
katta xavfi ombor qoldig'ining eskirib qolishi: agar ikki kassir bir xil
oxirgi donani ketma-ket sotsa, MoySklad buni darhol bilmaydi. Shu sabab
navbatga qo'yilgan HAR BIR buyurtma miqdori **darhol**, xotirada
(`backend/app/stock_cache.py`) MoySklad'ning jonli qoldig'idan ayirib
ko'rsatiladi — "Omborda: N dona" har doim navbatdagi buyurtmalarni hisobga
olib ko'rsatiladi, garchi ular hali MoySklad'ning o'zida yo'q bo'lsa ham.

Kassirlar "Tarix" ekranida hali sinxronlanmagan buyurtmalarni "Kutilmoqda"
statusi bilan ko'radi va ular sinxronlanmaguncha erkin tahrirlashi
(miqdorini o'zgartirish) yoki butunlay bekor qilishi mumkin.

**Har bir buyurtma AYNAN o'sha buyurtmani kiritgan kassirning o'z nomidan**
MoySklad'ga yuboriladi (bitta umumiy/admin hisob emas) — shunday qilib
MoySklad'da qaysi kassir nimani sotgani hozirgidek aniq ko'rinadi. Buning
uchun kassirning paroli (`crypto.py`, Fernet bilan shifrlangan holda) checkout
paytida Sheets qatoriga yozib qo'yiladi, sync esa uni hal qilib, aynan shu
kassir uchun yangi token oladi. **Muhim oqibat**: agar shu kassir sync
ishlagan payt (00:00/06:00/12:00/18:00) ilovada ham faol bo'lsa, MoySklad bir
login uchun faqat bitta faol token saqlagani sabab uning joriy sessiyasi
kutilmaganda uzilib, qayta kirishga to'g'ri kelishi mumkin — bu ongli
ravishda qabul qilingan almashinuv (kassir izini saqlash uchun).

### Sozlash

1. **Google Cloud Console**: yangi loyiha (yoki mavjudini tanlang), **Google
   Sheets API**'ni yoqing.
2. **Google Sheets'ga ulanish uchun kalit** — ikki usul bor, birinchisini
   sinab ko'ring:
   - **OAuth (tavsiya)**: ko'pgina yangi Google Cloud loyihalarida "Secure by
     Default" siyosati service-account KALIT yaratishni butunlay bloklab
     qo'yadi ("Service account key creation is disabled" xatosi). Bunday
     holda: "APIs & Services" → "Credentials" → "Create Credentials" →
     "OAuth client ID" → turi **"Desktop app"**. Chiqqan Client ID/Secret'ni
     `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`ga qo'ying, so'ng
     bir martalik brauzer orqali login qilib `refresh_token` oling (buni
     avtomatlashtirish uchun `backend`dagi yordamchi skriptni yoki qo'lda
     OAuth "authorization code" oqimini ishlatish mumkin) va
     `GOOGLE_OAUTH_REFRESH_TOKEN`ga qo'ying. **Eslatma**: OAuth consent
     screen "Testing" holatida bo'lsa, o'z emailingizni "Test users"ga
     qo'shishingiz kerak, aks holda "access_denied" xatosi chiqadi.
   - **Service-account** (agar policy ruxsat bersa): IAM & Admin → Service
     Accounts orqali yarating, JSON kalit yuklab oling, uni base64'ga
     o'giring (`[Convert]::ToBase64String([IO.File]::ReadAllBytes("kalit.json"))`)
     va `GOOGLE_SERVICE_ACCOUNT_JSON_B64`ga qo'ying. Bu holda Sheet'ni
     service-account'ning `client_email` manziliga Editor huquqi bilan
     ulashish HAM kerak — OAuth usulida bu shart emas, chunki Sheet
     to'g'ridan-to'g'ri sizning hisobingiz nomidan yaratiladi/ochiladi.
3. Google Sheet'ni yarating (yoki OAuth orqali API bilan yaratiladi), ID'ni
   URL'dan olib `GOOGLE_SHEETS_SPREADSHEET_ID`ga qo'ying.
4. `SYNC_TRIGGER_SECRET` yarating (`python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   va Render'ga qo'ying.
5. `CREDENTIAL_ENCRYPTION_KEY` — kassirlarning parolini (Sheets qatorida)
   shifrlash uchun kalit: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Bu bo'lmasa, "queue" rejimidagi checkout xato qaytaradi (chunki
   sinxronlash uchun kimning nomidan yuborishni bilmaydi).
6. `EXPECTED_MS_ORGANIZATION_ID` — MoySklad'dagi `entity/organization` UUID'i
   (MoySklad'ning o'zida yoki API orqali topish mumkin).
7. Google Sheet ichida: **Extensions → Apps Script**, quyidagi kabi funksiya
   qo'shing (BACKEND_URL/SYNC_SECRET qiymatlarini kodga yozmang — Project
   Settings → Script Properties'ga saqlang):

   ```javascript
   function triggerSync() {
     const props = PropertiesService.getScriptProperties();
     const res = UrlFetchApp.fetch(props.getProperty('BACKEND_URL') + '/api/sync/run', {
       method: 'post',
       headers: { 'X-Sync-Secret': props.getProperty('SYNC_SECRET') },
       muteHttpExceptions: true,
     });
     Logger.log(res.getContentText());
   }

   function installTriggers() {
     ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
     [0, 6, 12, 18].forEach(hour => {
       ScriptApp.newTrigger('triggerSync').timeBased().atHour(hour).everyDays(1).create();
     });
   }
   ```

   **Muhim**: trigger yaratishdan OLDIN loyihaning **Project Settings → Time
   zone**'ini `Asia/Tashkent`ga o'rnating — aks holda `atHour(H)` GMT+5 emas,
   loyihaning standart vaqt zonasi bo'yicha ishga tushadi. `installTriggers()`'ni
   Apps Script muharririda **bir marta** qo'lda ishga tushiring (har safar
   ishga tushirilsa, eski trigger'lar tozalanib qayta yaratiladi — bu funksiya
   shunga mo'ljallangan).
9. Hammasi tayyor bo'lgach, `CHECKOUT_MODE=queue` qiling va Render'ga deploy
   qiling. **Tavsiya**: avval `CHECKOUT_MODE=direct` bilan deploy qilib,
   `POST /api/sync/run`'ni qo'lda (masalan curl bilan, `X-Sync-Secret` header
   bilan) sinab ko'ring, keyin `queue`ga o'tkazing — bu haqiqiy pul/ombor
   bilan ishlaydigan tizim, shoshilinch emas.

### Bilinadigan cheklovlar

- Faqat bitta MoySklad tashkiloti uchun ishlaydi (bitta Sheet).
- Sync har bir buyurtmani o'sha kassirning o'z login-paroli bilan yuboradi —
  agar sync ishlagan payt (00:00/06:00/12:00/18:00) o'sha kassir ilovada
  faol bo'lsa, uning sessiyasi kutilmaganda uzilishi mumkin (yuqoridagi
  "Nega kerak..." bo'limiga qarang).
- Agar kassir MoySklad parolini o'zgartirsa, undan OLDIN navbatga qo'yilgan
  (hali sinxronlanmagan) buyurtmalar eski parol bilan sinxronlanishga
  urinadi va "Xato" (`failed`) holatiga tushadi — bunday holatda buyurtmani
  o'chirib, kassir qaytadan kiritishi kerak bo'ladi.
- Sinxronlash jarayoni navbatdagi buyurtmani yaratishda kutilmagan xatoga
  uchrasa (masalan MoySklad'dagi orqaga qaytarish — rollback — o'zi ham
  muvaffaqiyatsiz tugasa), qator "Tekshirish kerak" (`needs_manual_check`)
  holatiga o'tadi — bu holat uchun ilovada alohida ekran yo'q, Sheet'ning
  o'zida qatorni qo'lda ko'rib chiqish kerak bo'ladi (qatordagi
  `ms_order_id`/`externalCode` orqali MoySklad'da qidiring).
- Render bepul tarifi harakatsizlikdan keyin "uxlab qoladi" — Apps
  Script trigger'i shu paytga to'g'ri kelsa, so'rov vaqt tugashi (timeout)
  bilan tugashi mumkin. Bu xavfli emas: buyurtma "Kutilmoqda" holatida
  qolaveradi, keyingi (6 soatdan keyingi) trigger uni qayta sinab ko'radi.

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
