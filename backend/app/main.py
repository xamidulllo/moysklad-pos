"""Mobil POS uchun FastAPI backend — MoySklad API oldida proksi va biznes-mantiq qatlami.

Marshrutlar:
  POST /api/login           — kassir MoySklad login/paroli bilan kiradi (sessiya ochiladi)
  POST /api/logout          — joriy sessiyani yopadi
  GET  /api/me               — joriy kassir haqida ma'lumot
  GET  /api/products        — mahsulotlarni nomi/kod/artikul bo'yicha qidirish (entity/assortment)
  GET  /api/products/scan   — barkod bo'yicha aniq moslikni topish (kamera-skaner uchun)
  GET  /api/counterparties  — mijozlarni qidirish (entity/counterparty)
  POST /api/counterparties  — yangi mijoz (kontakt) yaratish
  GET  /api/accounts        — tashkilot hisoblarini olish (entity/organization/{id}/accounts)
  GET  /api/context         — tashkilotlar va omborlar ro'yxati (entity/organization, entity/store)
  GET  /api/currencies      — tashkilotda sozlangan valyutalar ro'yxati (entity/currency)
  GET  /api/projects        — loyihalar ro'yxati (entity/project)
  POST /api/checkout        — customerorder -> demand -> to'lov (cashin/paymentin) zanjirini yaratadi

Har bir /api/* (login'dan tashqari) marshrut joriy kassir sessiyasini talab qiladi —
MoySklad'ga so'rov shu kassirning shaxsiy tokeni bilan yuboriladi (auth.py'ga qarang).
"""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Awaitable, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import create_session, delete_session, get_current_session, get_current_token
from .bot import start_bot, stop_bot
from .config import SESSION_COOKIE_NAME, SESSION_COOKIE_SECURE, SESSION_TTL_HOURS
from .moysklad_client import exchange_credentials_for_token, ms_request
from .schemas import CheckoutRequest, CounterpartyCreate, LoginRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Telegram bot FastAPI bilan bitta process ichida, fon vazifasi sifatida ishga
    # tushadi — BOT_TOKEN sozlanmagan bo'lsa (lokal dev), jim o'tkazib yuboriladi.
    await start_bot()
    yield
    await stop_bot()


app = FastAPI(title="MoySklad Mobile POS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_headers(request, call_next):
    """Ilova faol rivojlantirilmoqda — brauzer/Telegram WebView/oraliq keshlar
    hech qanday javobni saqlab qolmasligi kerak, aks holda kod yangilansa ham
    foydalanuvchi eskirgan versiyani ko'raverishi mumkin (bu allaqachon Service
    Worker bilan bir marta sodir bo'lgan edi). Shu sabab HAR BIR javobga eng
    qattiq "keshlama" ko'rsatmasi qo'yiladi."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

# Oddiy xotiradagi kesh: hisoblar/tashkilotlar/omborlar har so'rovda o'zgarmaydi,
# shuning uchun kassirning har bir checkout ekraniga kirishida MoySklad'ga urilmaymiz.
# Kalitga token qo'shilgan — turli kassirlar (turli MoySklad hisoblari/huquqlari)
# bir-birining keshlangan ma'lumotini ko'rmasligi uchun.
_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 60


async def _cached(key: str, token: str, loader: Callable[[], Awaitable[dict]]) -> dict:
    cache_key = f"{key}:{token}"
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = await loader()
    _cache[cache_key] = (now, value)
    return value


def _to_minor_units(sum_in_som: float) -> int:
    """MoySklad summalarni tiyinda (kopeykada) kutadi: 1 so'm = 100 birlik."""
    return round(sum_in_som * 100)


def _id_from_href(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]


async def _get_default_currency_id(token: str) -> "str | None":
    """Tashkilotning bazaviy (учетная) valyutasi ID'sini qaytaradi.

    MoySklad qo'lda kiritilgan kursni ("rate.value") faqat bazaviy valyutadan
    FARQLI valyutadagi to'lovlar uchun qabul qiladi — aks holda xato 3007
    ("Нельзя задать курс валюты учета, отличный от 1") qaytaradi. Shu sabab
    checkout paytida tanlangan hisob valyutasini shu bilan solishtiramiz.
    """

    async def loader():
        data = await ms_request("GET", "/entity/currency", token=token, params={"limit": 100})
        default_row = next((r for r in data.get("rows", []) if r.get("default")), None)
        return {"id": default_row["id"] if default_row else None}

    result = await _cached("default_currency", token, loader)
    return result["id"]


# MoySklad har bir yangi hisobda avtomatik yaratadigan standart "Цена продажи"
# narx turining externalCode'i — hisoblar orasida barqaror qiymat (tizim tomonidan
# beriladi), shuning uchun nomi o'zgartirilgan yoki tarjima qilingan taqdirda ham
# ishonchli aniqlash uchun ishlatiladi.
_DEFAULT_PRICE_TYPE_EXTERNAL_CODE = "cbcf493b-55bc-11d9-848a-00112f43529a"


async def _get_default_price_type_id(token: str) -> "str | None":
    """Tovarlarning bir nechta narx turi bo'lishi mumkin (masalan "Цена продажи"
    dollarda, kassir o'zi qo'shgan "Do'kon Sotov" so'mda) — POS uchun har doim
    MoySklad'ning standart "Цена продажи" turini ishlatish kerak, aks holda
    qaysi narx tasodifan array'da birinchi kelsa, o'sha noto'g'ri olinadi.
    """

    async def loader():
        data = await ms_request("GET", "/context/companysettings", token=token)
        price_types = data.get("priceTypes") or []
        match = next(
            (
                pt
                for pt in price_types
                if pt.get("externalCode") == _DEFAULT_PRICE_TYPE_EXTERNAL_CODE
                or pt.get("name") == "Цена продажи"
            ),
            None,
        )
        return {"id": match["id"] if match else None}

    result = await _cached("default_price_type", token, loader)
    return result["id"]


def _pick_sale_price(sale_prices: list, default_price_type_id: "str | None") -> "dict | None":
    if not sale_prices:
        return None
    if default_price_type_id:
        for sp in sale_prices:
            price_type = sp.get("priceType") or {}
            if price_type.get("id") == default_price_type_id:
                return sp
    # Standart tur topilmasa (masalan kompaniya uni o'chirib tashlagan bo'lsa),
    # eng yomon holatda ham xato chiqmasligi uchun birinchi narxga qaytamiz.
    return sale_prices[0]


_YES_LIKE_NAMES = ("ha", "да", "yes", "true", "тўғри", "to'g'ri")


async def _get_required_order_attributes(token: str) -> list:
    """Ba'zi MoySklad hisoblarida buyurtma (customerorder) uchun qo'shimcha
    maydonlar (custom attributes) "majburiy" deb belgilangan bo'ladi — bunday
    hisoblarda ularsiz order yaratib bo'lmaydi (MoySklad butunlay rad etadi).
    Bu funksiya har doim shu hisobning HAQIQIY majburiy maydonlarini o'zi
    aniqlab, ularga mantiqiy standart qiymat beradi — hech qanday maydon nomi
    yoki ID kodda qattiq yozilmagan, shuning uchun boshqa MoySklad hisobida
    boshqa (yoki hech qanday) majburiy maydon bo'lsa ham avtomatik moslashadi.

    Faqat ikki turdagi maydon uchun ishonchli standart tanlanadi:
      - "boolean" — True qilib qo'yiladi;
      - "customentity" (lug'atdan tanlash, masalan "Ha"/"Yo'q") — "Ha"ga
        o'xshash nomli variant, aks holda yagona/birinchi variant tanlanadi.
    Boshqa turdagi (matn, sana va h.k.) majburiy maydonlar uchun ishonchli
    standart yo'q — ular o'tkazib yuboriladi (kerak bo'lsa administrator shu
    maydonni "majburiy emas" qilishi kerak bo'ladi).
    """

    async def loader():
        try:
            data = await ms_request(
                "GET", "/entity/customerorder/metadata/attributes", token=token, params={"limit": 1000}
            )
        except HTTPException:
            # Ba'zi MoySklad tariflarida qo'shimcha maydonlar funksiyasi umuman
            # yo'q — bunday hisoblarda so'rovning o'zi xato qaytaradi (masalan
            # "Тарифное ограничение"). Bu checkout'ni butunlay to'xtatmasligi
            # kerak — shunchaki majburiy maydon yo'q deb hisoblanadi.
            return {"items": []}
        items = []
        for attr in data.get("rows", []):
            if not attr.get("required"):
                continue
            attr_meta = attr["meta"]

            if attr.get("type") == "boolean":
                items.append({"meta": attr_meta, "value": True})
                continue

            if attr.get("type") == "customentity":
                custom_entity_href = (attr.get("customEntityMeta") or {}).get("href", "")
                custom_entity_id = _id_from_href(custom_entity_href) if custom_entity_href else None
                if not custom_entity_id:
                    continue
                try:
                    dict_data = await ms_request(
                        "GET", f"/entity/customentity/{custom_entity_id}", token=token, params={"limit": 100}
                    )
                except HTTPException:
                    continue
                options = dict_data.get("rows", [])
                if not options:
                    continue
                chosen = next(
                    (o for o in options if o.get("name", "").strip().lower() in _YES_LIKE_NAMES),
                    options[0],
                )
                items.append({"meta": attr_meta, "value": {"meta": chosen["meta"], "name": chosen.get("name")}})

        return {"items": items}

    result = await _cached("required_order_attributes", token, loader)
    return result["items"]


# ---------------------------------------------------------------------------
# Autentifikatsiya
# ---------------------------------------------------------------------------


@app.post("/api/login")
async def login(payload: LoginRequest, response: Response):
    token = await exchange_credentials_for_token(payload.login, payload.password)
    employee = await ms_request("GET", "/context/employee", token=token)
    employee_name = employee.get("name") or employee.get("fullName") or payload.login

    session_id = create_session(token, employee_name)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        max_age=int(SESSION_TTL_HOURS * 3600),
    )
    return {"employee_name": employee_name}


@app.post("/api/logout")
async def logout(response: Response, pos_session: Optional[str] = Cookie(default=None)):
    delete_session(pos_session)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
async def me(session: dict = Depends(get_current_session)):
    return {"employee_name": session["employee_name"]}


# ---------------------------------------------------------------------------
# Katalog / spravochniklar
# ---------------------------------------------------------------------------


def _extract_image_url(row: dict) -> "str | None":
    """Tovar rasmining kichik (tiny) nusxasi URL'ini qaytaradi.

    MUHIM (real hisobda tekshirilgan, o'zim rasm yuklab ko'rdim): rasmning
    "tiny"/"miniature" havolalari MoySklad'ning asosiy API domenida EMAS,
    balki alohida "tinyimage-prod.moysklad.ru" kabi domenda joylashgan va
    HECH QANDAY autentifikatsiya talab qilmaydi (ochiq, imzolangan URL) —
    shu sabab backend orqali "proxy" qilishning hojati yo'q, frontend to'g'ridan-
    to'g'ri shu URL'dan foydalanadi.
    """
    images = row.get("images")
    if not isinstance(images, dict):
        return None
    image_rows = images.get("rows")
    if not image_rows:
        return None
    tiny = image_rows[0].get("tiny") or {}
    return tiny.get("href")


def _assortment_row_to_item(row: dict, default_price_type_id: "str | None") -> dict:
    sale_prices = row.get("salePrices") or []
    # ESLATMA (haqiqiy hisobda tekshirilgan): bir tovarda bir nechta narx turi
    # bo'lishi mumkin (masalan "Цена продажи" dollarda, do'kon o'zi qo'shgan
    # boshqa tur so'mda) — array'dagi BIRINCHI narxni emas, aynan MoySklad'ning
    # standart "Цена продажи" turini olish kerak, aks holda tasodifiy noto'g'ri
    # valyutadagi narx tanlanadi.
    chosen = _pick_sale_price(sale_prices, default_price_type_id)
    price = (chosen["value"] / 100) if chosen else 0
    price_currency = chosen.get("currency") if chosen else None
    price_currency_id = _id_from_href(price_currency["meta"]["href"]) if price_currency else None
    return {
        "id": row.get("id"),
        "meta": row.get("meta"),
        "name": row.get("name"),
        "code": row.get("code"),
        "article": row.get("article"),
        "price": price,
        "price_currency_id": price_currency_id,
        "image_url": _extract_image_url(row),
        "type": (row.get("meta") or {}).get("type"),
    }


@app.get("/api/products")
async def search_products(q: str = Query("", alias="q"), token: str = Depends(get_current_token)):
    """Mahsulotlarni nomi, kodi yoki artikuli bo'yicha qidiradi.

    ESLATMA (real API'da tekshirilgan): "entity/assortment" endpoint'i "search"
    parametrini JIM RAVISHDA e'tiborga olmaydi (har doim to'liq ro'yxatni
    qaytaradi) — MoySklad'ning boshqa ko'p entity'laridan farqli xatti-harakat.
    Shu sabab bu yerda "filter=maydon~qiymat" sintaksisi ishlatiladi; turli
    maydonlar bo'yicha filter'lar MoySklad'da AND birlashtiriladi (OR emas),
    shuning uchun har bir maydon uchun alohida so'rov yuborilib, natijalar
    birlashtiriladi (OR semantikasini qo'lda hosil qilish).
    """
    default_price_type_id = await _get_default_price_type_id(token)

    if not q:
        data = await ms_request(
            "GET", "/entity/assortment", token=token, params={"limit": 50, "expand": "images"}
        )
        return {"items": [_assortment_row_to_item(r, default_price_type_id) for r in data.get("rows", [])]}

    results = await asyncio.gather(
        *[
            ms_request(
                "GET",
                "/entity/assortment",
                token=token,
                params={"filter": f"{field}~{q}", "limit": 50, "expand": "images"},
            )
            for field in ("name", "code", "article")
        ]
    )

    merged: dict[str, dict] = {}
    for data in results:
        for row in data.get("rows", []):
            merged[row["id"]] = row

    return {"items": [_assortment_row_to_item(r, default_price_type_id) for r in merged.values()]}


@app.get("/api/products/scan")
async def scan_product(code: str = Query(..., min_length=1), token: str = Depends(get_current_token)):
    """Kamera bilan o'qilgan barkod bo'yicha ANIQ moslikni qidiradi.

    Real API'da tekshirilgan: "filter=barcode=<qiymat>" barkodlar massivi
    ("barcodes": [{"ean13": "..."}]) ichidan aniq moslikni topadi.
    """
    data = await ms_request(
        "GET",
        "/entity/assortment",
        token=token,
        params={"filter": f"barcode={code}", "limit": 1, "expand": "images"},
    )
    rows = data.get("rows", [])
    if not rows:
        return {"item": None}
    default_price_type_id = await _get_default_price_type_id(token)
    return {"item": _assortment_row_to_item(rows[0], default_price_type_id)}


@app.get("/api/counterparties")
async def search_counterparties(q: str = Query("", alias="q"), token: str = Depends(get_current_token)):
    params = {"limit": 20}
    if q:
        params["search"] = q
    data = await ms_request("GET", "/entity/counterparty", token=token, params=params)
    return {
        "items": [
            {"id": row["id"], "meta": row["meta"], "name": row["name"]}
            for row in data.get("rows", [])
        ]
    }


@app.post("/api/counterparties")
async def create_counterparty(payload: CounterpartyCreate, token: str = Depends(get_current_token)):
    """Yangi mijoz (kontakt) yaratadi — real API'da tekshirilgan, faqat 'name' majburiy."""
    body = {"name": payload.name}
    if payload.phone:
        body["phone"] = payload.phone
    row = await ms_request("POST", "/entity/counterparty", token=token, json=body)
    return {"id": row["id"], "meta": row["meta"], "name": row["name"]}


@app.get("/api/projects")
async def get_projects(token: str = Depends(get_current_token)):
    """Buyurtmaga biriktiriladigan loyihalar (Проекты) ro'yxati — real API'da tekshirilgan."""

    async def loader():
        try:
            data = await ms_request("GET", "/entity/project", token=token, params={"limit": 100})
        except HTTPException:
            # Ba'zi MoySklad tariflarida bu funksiya cheklangan bo'lishi mumkin —
            # bunday holda checkout ekranini butunlay to'xtatmasdan, shunchaki
            # loyiha tanlovi yashirin qoladi (aynan attribute'lar uchun qilingan
            # fallback bilan bir xil naqsh).
            return {"items": []}
        return {
            "items": [
                {"id": r["id"], "meta": r["meta"], "name": r["name"]} for r in data.get("rows", [])
            ]
        }

    return await _cached("projects", token, loader)


CASH_KEYWORDS = ("kassa", "касс", "нал", "naqd", "cash")


@app.get("/api/accounts")
async def get_accounts(token: str = Depends(get_current_token)):
    """Kassir uchun to'lov hisoblari ro'yxatini qaytaradi.

    MoySklad'da alohida "kassa" entity'si yo'q — naqd va bank hisoblari bitta
    "entity/organization/{id}/accounts" to'plamida saqlanadi (real API'da
    tekshirilgan). Shu sabab hisoblar har bir tashkilot bo'yicha alohida
    so'raladi va natijalar birlashtiriladi. "cash"/"bank" turi MoySklad'da
    aniq maydon sifatida kelmaydi, shuning uchun hisob nomiga qarab taxminiy
    belgilanadi — kassir buni to'lov ekranida qo'lda tasdiqlaydi/o'zgartiradi.
    """

    async def loader():
        default_currency_id = await _get_default_currency_id(token)
        orgs = await ms_request("GET", "/entity/organization", token=token, params={"limit": 100})
        items = []
        for org in orgs.get("rows", []):
            org_id = org["id"]
            accounts = await ms_request(
                "GET", f"/entity/organization/{org_id}/accounts", token=token, params={"limit": 100}
            )
            for row in accounts.get("rows", []):
                label = row.get("bankName") or row.get("accountNumber") or "Hisob"
                lowered = label.lower()
                guessed_type = "cash" if any(k in lowered for k in CASH_KEYWORDS) else "bank"
                currency = row.get("currency")
                currency_id = _id_from_href(currency["meta"]["href"]) if currency else None
                items.append(
                    {
                        "id": row["id"],
                        "meta": row["meta"],
                        "name": label,
                        "is_default": row.get("isDefault", False),
                        "guessed_type": guessed_type,
                        "currency": currency,
                        # Bazaviy valyutadagi hisoblarda qo'lda kurs kiritib bo'lmaydi (MoySklad xato 3007)
                        "is_base_currency": currency_id == default_currency_id,
                        "organization_id": org_id,
                    }
                )
        return {"items": items}

    return await _cached("accounts", token, loader)


@app.get("/api/context")
async def get_context(token: str = Depends(get_current_token)):
    """Buyurtma/otgruzka uchun majburiy bo'lgan tashkilot va ombor ro'yxatlarini qaytaradi."""

    async def loader():
        orgs = await ms_request("GET", "/entity/organization", token=token, params={"limit": 100})
        stores = await ms_request("GET", "/entity/store", token=token, params={"limit": 100})
        return {
            "organizations": [
                {"id": r["id"], "meta": r["meta"], "name": r["name"]} for r in orgs.get("rows", [])
            ],
            "stores": [
                {"id": r["id"], "meta": r["meta"], "name": r["name"]} for r in stores.get("rows", [])
            ],
        }

    return await _cached("context", token, loader)


@app.get("/api/currencies")
async def get_currencies(token: str = Depends(get_current_token)):
    """Tashkilotda sozlangan barcha valyutalarni qaytaradi (masalan so'm, dollar, rubl —
    aniq nechta va qaysilari ekani hisobga qarab farq qiladi, kodda qattiq yozilmagan).
    Frontend shu ro'yxat asosida har bir tovar/kurs uchun valyuta tanlovlarini quradi."""

    async def loader():
        data = await ms_request("GET", "/entity/currency", token=token, params={"limit": 100})
        return {
            "items": [
                {
                    "id": r["id"],
                    "meta": r["meta"],
                    "name": r.get("name") or r.get("isoCode") or "Valyuta",
                    "iso_code": r.get("isoCode"),
                    "is_default": bool(r.get("default")),
                }
                for r in data.get("rows", [])
            ]
        }

    return await _cached("currencies", token, loader)


# ---------------------------------------------------------------------------
# Checkout: Заказ покупателя -> Otgruzka -> To'lov
# ---------------------------------------------------------------------------


@app.post("/api/checkout")
async def checkout(payload: CheckoutRequest, token: str = Depends(get_current_token)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    # MUHIM (real API'da to'g'ridan-to'g'ri tekshirilgan): "positions[].price"
    # hujjatning O'Z rate.currency birligida ishlatiladi — MoySklad uni bazaviy
    # valyutaga hech qanday avtomatik konvertatsiya qilmaydi. Shu sabab frontend
    # narxni to'g'ridan-to'g'ri kassir tanlagan valyutada yuboradi, bu yerda
    # hech qanday qo'shimcha konvertatsiya qilinmaydi.
    positions = [
        {
            "quantity": item.quantity,
            "price": _to_minor_units(item.price),
            "assortment": {"meta": item.assortment_meta},
        }
        for item in payload.items
    ]

    # Kassir chet el valyutasini tanlagan bo'lsa, shu bitta "rate" BARCHA UCH
    # hujjatga (buyurtma, otgruzka, to'lov) baravar qo'llaniladi — aks holda
    # ular turli valyutada chiqib, chalkashlik yuzaga kelardi (haqiqiy
    # foydalanishda aynan shu xato tasdiqlangan edi). MUHIM: MoySklad
    # tashkilotning bazaviy (учетная) valyutasi uchun rate.value != 1
    # yuborilsa xato 3007 bilan rad etadi — kurs faqat CHET EL valyutasidagi
    # hisoblarda qo'llaniladi.
    document_rate = None
    if payload.exchange_rate and payload.exchange_rate > 0 and payload.currency_meta:
        selected_currency_id = _id_from_href(payload.currency_meta.get("href", ""))
        default_currency_id = await _get_default_currency_id(token)
        if selected_currency_id != default_currency_id:
            # MUHIM (MoySklad interfeysida vizual tasdiqlangan): API'ning
            # "rate.value" maydoni kassir kiritgan "1 bazaviy = X hujjat valyutasi"
            # (masalan "1 dollar = 12000 so'm") yo'nalishining TESKARISIDA
            # saqlanadi. MoySklad o'z interfeysida "1 USD = 12 000 UZS" to'g'ri
            # ko'rsatishi uchun bu yerga 1/12000 yuborilishi kerak — kassir
            # kiritgan raqamning o'zi emas.
            document_rate = {
                "value": 1 / payload.exchange_rate,
                "currency": {"meta": payload.currency_meta},
            }

    # 1) Заказ покупателя (customerorder) — POS-sotuvning boshlang'ich hujjati.
    # Otgruzka va to'lov ikkalasi ham shunga bog'lanadi (pastga qarang).
    order_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "store": {"meta": payload.store_meta},
        "positions": positions,
        "applicable": True,
    }
    if document_rate:
        order_body["rate"] = document_rate
    if payload.comment:
        order_body["description"] = payload.comment

    # Ba'zi hisoblarda buyurtma uchun majburiy qo'shimcha maydonlar (custom
    # attributes) sozlangan bo'ladi — ularsiz MoySklad order yaratishni rad
    # etadi. Bu yerda ular avtomatik topilib, mantiqiy standart qiymat bilan
    # to'ldiriladi (real hisobda tekshirilgan).
    required_attrs = await _get_required_order_attributes(token)
    if required_attrs:
        order_body["attributes"] = required_attrs
    if payload.project_meta:
        order_body["project"] = {"meta": payload.project_meta}
    order = await ms_request("POST", "/entity/customerorder", token=token, json=order_body)
    order_sum = order.get("sum", 0)

    # 2) Otgruzka (demand) — buyurtmadan yaratiladi, "customerOrder" maydoni orqali
    # unga bog'lanadi (real API'dagi mavjud hujjatlarda tekshirilgan maydon nomi).
    demand_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "store": {"meta": payload.store_meta},
        "positions": positions,
        "applicable": True,
        "customerOrder": {"meta": order["meta"]},
    }
    if document_rate:
        demand_body["rate"] = document_rate
    if payload.comment:
        demand_body["description"] = payload.comment
    if payload.project_meta:
        demand_body["project"] = {"meta": payload.project_meta}
    try:
        demand = await ms_request("POST", "/entity/demand", token=token, json=demand_body)
    except HTTPException:
        # Otgruzka yaratilmasa, endi hech narsaga bog'lanmagan buyurtma qolib ketmasin.
        await ms_request("DELETE", f"/entity/customerorder/{order['id']}", token=token)
        raise

    # 3) To'lov hujjati — QARZGA sotuvda umuman yaratilmaydi (faqat buyurtma +
    # otgruzka qoladi, mijoz qarzi MoySklad'da to'lanmagan buyurtma sifatida ko'rinadi).
    if payload.is_debt:
        return {
            "order": {"id": order["id"], "name": order.get("name")},
            "demand": {"id": demand["id"], "name": demand.get("name")},
            "payment": None,
        }

    # Kassir tanlagan hujjat turiga qarab naqd (cashin) yoki bank (paymentin).
    # Foydalanuvchi tanloviga ko'ra BUYURTMAning o'ziga bog'lanadi
    # ("operations[].linkedSum"), otgruzkaga emas.
    # ESLATMA (real API'da tekshirilgan): "paymentin" hisob bog'lanishini
    # ("organizationAccount") to'liq saqlaydi, lekin "cashin" (ПКО) MoySklad'da
    # muayyan hisobga umuman bog'lanmaydi — u faqat tashkilotning umumiy kassa
    # balansini oshiradi, shuning uchun organizationAccount maydoni cashin uchun
    # jo'natilsa ham e'tiborga olinmaydi (xato bermaydi, shunchaki saqlanmaydi).
    payment_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "applicable": True,
        "sum": order_sum,
        "organizationAccount": {"meta": payload.account_meta},
        "operations": [{"meta": order["meta"], "linkedSum": order_sum}],
    }
    if document_rate:
        payment_body["rate"] = document_rate

    # Kartada oldindan to'langan holatlar uchun — to'lov sanasi/vaqti sotuv
    # vaqtidan farq qilishi mumkin, kassir buni qo'lda ko'rsata oladi
    # (real API'da tekshirilgan: "moment": "YYYY-MM-DD HH:MM:SS" formatida qabul qilinadi).
    if payload.payment_moment:
        payment_body["moment"] = payload.payment_moment

    payment_endpoint = "/entity/cashin" if payload.document_type == "cashin" else "/entity/paymentin"
    try:
        payment = await ms_request("POST", payment_endpoint, token=token, json=payment_body)
    except HTTPException:
        # To'lov yaratilmasa, endi bog'lanmagan otgruzka va buyurtma tizimda
        # "osilib qolmasligi" kerak — aks holda kassir xato ko'radi-yu, lekin
        # omborda tasdiqlangan otgruzka/buyurtma qolib ketadi.
        await ms_request("DELETE", f"/entity/demand/{demand['id']}", token=token)
        await ms_request("DELETE", f"/entity/customerorder/{order['id']}", token=token)
        raise

    return {
        "order": {"id": order["id"], "name": order.get("name")},
        "demand": {"id": demand["id"], "name": demand.get("name")},
        "payment": {"id": payment["id"], "name": payment.get("name")},
    }


# Frontend'ni backend bilan bitta origin'dan xizmat ko'rsatish — PWA va CORS uchun qulay.
# Bu mount har doim eng oxirida bo'lishi kerak, aks holda yuqoridagi /api/* marshrutlarni bosib qo'yadi.
_frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
