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
  GET  /api/orders/history  — shu ilova orqali kiritilgan buyurtmalar tarixi (saleschannel bo'yicha)

Endi checkout ikki rejimda ishlashi mumkin (config.CHECKOUT_MODE):
  - "direct" (standart) — hozirgidek, checkout darhol MoySklad'ga yozadi.
  - "queue" — FAQAT config.EXPECTED_MS_ORGANIZATION_ID'ga mos MoySklad hisobi
    uchun: checkout MoySklad'ga tegmasdan Google Sheets navbatiga yozadi,
    davriy sync (sync_job.py, Google Apps Script trigger orqali chaqiriladi)
    buni keyinroq MoySklad'ga ko'chiradi. Boshqa har qanday login uchun
    baravar to'g'ridan-to'g'ri yozadi.

Har bir /api/* (login'dan tashqari) marshrut joriy kassir sessiyasini talab qiladi —
MoySklad'ga so'rov shu kassirning shaxsiy tokeni bilan yuboriladi (auth.py'ga qarang).
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import catalog_cache, sheets_client, shop_sync, stock_cache, sync_job
from .auth import create_session, delete_session, get_current_session, get_current_token, require_sync_secret
from .bot import start_bot, stop_bot
from .cache import _cached
from .checkout_chain import _get_default_currency_id, _get_pos_sales_channel_meta, execute_checkout_chain
from .config import (
    CHECKOUT_MODE,
    DEFAULT_EXCHANGE_RATE,
    EXPECTED_MS_ORGANIZATION_ID,
    GOOGLE_SHEETS_SPREADSHEET_ID,
    MOYSKLAD_BASE_URL,
    MS_SYNC_LOGIN,
    MS_SYNC_PASSWORD,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_HOURS,
    SHOP_ALLOWED_ACCOUNT_NAMES,
    SHOP_ORGANIZATION_ID,
    SHOP_PRICE_TYPE_NAMES,
)
from .shop_day import business_day_key, now_in_shop_tz
from .moysklad_client import close_client as close_ms_client
from .moysklad_client import exchange_credentials_for_token, ms_request
from .schemas import CheckoutRequest, CounterpartyCreate, LoginRequest, PendingOrderItemsEdit
from .utils import _id_from_href

logger = logging.getLogger("moysklad_pos.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # "queue" rejimi Sheets'ni yagona ishonchli manba sifatida ishlatadi —
    # xotiradagi qoldiq keshi (stock_cache) esa har ishga tushishda undan
    # qaytadan quriladi (bu jarayon xotirasi, _sessions kabi, qayta ishga
    # tushirilganda yo'qoladi). Sheets vaqtincha ishlamasa ham ilova butunlay
    # to'xtab qolmasligi kerak (kassirlar hali ham kira olishi kerak) —
    # shuning uchun xatolik faqat logga yoziladi, ishga tushirish davom etadi.
    if CHECKOUT_MODE == "queue" and GOOGLE_SHEETS_SPREADSHEET_ID:
        try:
            rows = await sheets_client.get_all_rows()
            stock_cache.rebuild_from_rows(rows)
            logger.info("Ombor qoldiq keshi Sheets'dan tiklandi (%d qator)", len(rows))
        except Exception:
            logger.exception(
                "Ombor qoldiq keshini Sheets'dan tiklab bo'lmadi — davom etiladi, "
                "lekin navbatdagi buyurtmalar qoldiqqa hisobga olinmasligi mumkin"
            )
    # Tovar katalogini oldindan "isitib" qo'yamiz — aks holda qayta ishga
    # tushirilgandan keyin BIRINCHI qidiruv so'rovi to'liq katalogni (rasmlar
    # bilan) yuklashni kutib, ~20+ soniya davom etardi (catalog_cache.py'ga
    # qarang). Ilova ishga tushishini BLOKLAMASLIK uchun fonda, alohida
    # vazifada boshlanadi.
    if MS_SYNC_LOGIN and MS_SYNC_PASSWORD:
        async def _warm_catalog_cache():
            try:
                token = await sync_job.get_shared_admin_token()
                account_id = await _get_account_id(token)
                try:
                    await catalog_cache.ensure_fresh(account_id, token)
                except HTTPException as exc:
                    if exc.status_code not in (401, 403):
                        raise
                    # Umumiy token boshqa joyda (masalan shu login bilan deploy
                    # vaqtida bir nechta instance qisqa vaqt bir-birining ustidan
                    # chiqib ketishi natijasida) bekor qilingan bo'lishi mumkin —
                    # sync_job.py'dagi bilan bir xil naqsh: bir marta majburiy
                    # yangilab qayta uriniladi.
                    logger.warning("Isitish uchun token bekor qilingan (401/403) — majburiy yangilab qayta urinilmoqda")
                    token = await sync_job.get_shared_admin_token(force_refresh=True)
                    account_id = await _get_account_id(token)
                    await catalog_cache.ensure_fresh(account_id, token)
                logger.info("Katalog keshi oldindan isitildi (accountId=%s)", account_id)
            except Exception:
                logger.exception("Katalog keshini oldindan isitib bo'lmadi — birinchi qidiruv sekinroq bo'lishi mumkin")

        warm_task = asyncio.create_task(_warm_catalog_cache())
        catalog_cache._background_tasks.add(warm_task)
        warm_task.add_done_callback(catalog_cache._background_tasks.discard)

    # Telegram bot FastAPI bilan bitta process ichida, fon vazifasi sifatida ishga
    # tushadi — BOT_TOKEN sozlanmagan bo'lsa (lokal dev), jim o'tkazib yuboriladi.
    await start_bot()
    yield
    await stop_bot()
    await close_ms_client()


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

# MoySklad har bir yangi hisobda avtomatik yaratadigan standart "Цена продажи"
# narx turining externalCode'i — hisoblar orasida barqaror qiymat (tizim tomonidan
# beriladi), shuning uchun nomi o'zgartirilgan yoki tarjima qilingan taqdirda ham
# ishonchli aniqlash uchun ishlatiladi.
_DEFAULT_PRICE_TYPE_EXTERNAL_CODE = "cbcf493b-55bc-11d9-848a-00112f43529a"


async def _get_default_price_type_id(token: str) -> "str | None":
    """Tovarlarning bir nechta narx turi bo'lishi mumkin (masalan "Цена продажи"
    dollarda, do'kon o'zi qo'shgan "Do'kon sotuv" so'mda). Avval
    config.SHOP_PRICE_TYPE_NAMES ro'yxatidagi nomlar tartib bilan qidiriladi
    (masalan "Do'kon sotuv"); hech biri topilmasa, MoySklad'ning standart
    "Цена продажи" turiga tushiladi — aks holda qaysi narx tasodifan
    array'da birinchi kelsa, o'sha noto'g'ri olinadi.
    """

    async def loader():
        data = await ms_request("GET", "/context/companysettings", token=token)
        price_types = data.get("priceTypes") or []
        for name in SHOP_PRICE_TYPE_NAMES:
            match = next((pt for pt in price_types if pt.get("name") == name), None)
            if match:
                return {"id": match["id"]}
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


async def _get_account_id(token: str) -> str:
    """Katalog/mijozlar keshi (catalog_cache.py) MoySklad HISOBI (accountId)
    bo'yicha ajratilgan — bu ilova istalgan MoySklad login bilan kirishga
    ruxsat bergani uchun (multi-tenant), turli hisoblarning kassirlari
    bir-birining tovar/mijoz ro'yxatini ko'rib qolmasligi kerak."""

    async def loader():
        employee = await ms_request("GET", "/context/employee", token=token)
        return {"account_id": employee.get("accountId")}

    result = await _cached("account_id", token, loader)
    return result["account_id"]


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
    """Tovar rasmining kichraytirilgan nusxasi URL'ini qaytaradi.

    MUHIM (real hisobda tekshirilgan, o'zim rasm yuklab ko'rdim): MoySklad
    rasmning bir nechta o'lchamini beradi — "tiny" (juda kichik, sifatsiz —
    ro'yxatda deyarli tanib bo'lmaydi) va undan katta/sifatliroq "miniature".
    Ikkalasi ham MoySklad'ning asosiy API domenida EMAS, balki alohida
    "tinyimage-prod.moysklad.ru" / "miniature-prod.moysklad.ru" domenlarida
    joylashgan va HECH QANDAY autentifikatsiya talab qilmaydi — shu sabab
    backend orqali "proxy" qilishning hojati yo'q. "miniature.href" (asosiy
    API domenida) autentifikatsiya talab qiladi — shuning uchun aynan
    "miniature.downloadHref" ishlatiladi, "miniature.href" emas.
    """
    images = row.get("images")
    if not isinstance(images, dict):
        return None
    image_rows = images.get("rows")
    if not image_rows:
        return None
    first = image_rows[0]
    miniature = first.get("miniature") or {}
    tiny = first.get("tiny") or {}
    return miniature.get("downloadHref") or tiny.get("href")


def _assortment_row_to_item(
    row: dict,
    default_price_type_id: "str | None",
    store_id: "str | None" = None,
    stock_by_store: "dict[str, float] | None" = None,
) -> dict:
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

    # Faqat "store_id" (va shu ombor uchun oldindan yuklangan "stock_by_store"
    # xaritasi) berilganda hisoblanadi. Xaritada yo'q tovar — shu omborda
    # 0 dona (report o'zi ham nol qoldiqli tovarlarni chiqarmaydi). Bor bo'lsa,
    # Google Sheets navbatida hali MoySklad'ga sinxronlanmagan buyurtmalar
    # qoldirgan "deduksiya"ni ham shu yerda ayiramiz — aks holda navbatga
    # qo'yilgan, lekin hali MoySklad'ga yetib bormagan sotuv oxirgi donani
    # ikkinchi mijozga ham sotib qo'yishi mumkin edi.
    stock = None
    if store_id and stock_by_store is not None:
        stock = stock_by_store.get(row.get("id"), 0.0)
        stock = max(0.0, stock - stock_cache.get_deduction(store_id, row.get("id")))

    # MUHIM (tekshirish kerak): MoySklad odatda "uom"ni assortment qatorida
    # to'g'ridan-to'g'ri (expand'siz) "shт"/"л"/"м" kabi nom bilan qaytaradi.
    # Agar productionda "name" bo'sh chiqib qolsa, catalog_cache.py'dagi
    # assortment so'rovi "expand=uom" bilan ham to'ldirilishi kerak bo'ladi
    # (rasmlar uchun topilgan "limit>100 sinab ishlamay qoladi" muammosi
    # takrorlanmasligi uchun avval kichik sahifada sinab ko'rilsin).
    uom = row.get("uom") or {}
    uom_name = uom.get("name")

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
        "stock": stock,
        "uom_name": uom_name,
    }


async def _no_stock() -> None:
    """`asyncio.gather` bir xil shakldagi natijalar kutgani uchun — ombor
    tanlanmagan holatda "qoldiq xaritasi o'rniga" ishlatiladigan bo'sh qiymat."""
    return None


async def _get_stock_by_store(token: str, store_id: str) -> "dict[str, float]":
    """Tanlangan ombordagi HAR BIR tovarning aniq qoldig'ini {tovar_id: qoldiq}
    xaritasi sifatida qaytaradi.

    MUHIM (real hisobda tekshirilgan): "entity/assortment"ga "stockStore=<ombor
    href>" berish — avvalgi taxmin bo'yicha shu ombor bo'yicha aniq qoldiq
    qaytarishi kerak edi, LEKIN ba'zi tovarlar uchun (masalan bir nechta
    ombor/partiya harakati bo'lgan tovarlarda) buni SOQIB, barcha omborlar
    bo'yicha UMUMIY qoldiqni qaytarib yuborishi aniqlandi — bu kassirni real
    zaxiradan ko'ra ko'proq tovar sotib yuborishga olib kelishi mumkin edi.
    Shu sabab endi alohida, maxsus "report/stock/bystore" hisoboti
    ishlatiladi — bu hisobot shu ombordagi HAR BIR tovarni to'g'ri qoldig'i
    bilan birma-bir qaytaradi (real hisobda solishtirib tekshirilgan).
    """

    async def loader():
        store_href = f"{MOYSKLAD_BASE_URL}/entity/store/{store_id}"
        stock_map: dict[str, float] = {}
        offset = 0
        while True:
            data = await ms_request(
                "GET",
                "/report/stock/bystore",
                token=token,
                params={"filter": f"store={store_href}", "limit": 1000, "offset": offset},
            )
            rows = data.get("rows", [])
            for row in rows:
                product_id = _id_from_href((row.get("meta") or {}).get("href", ""))
                if not product_id:
                    continue
                entries = row.get("stockByStore") or []
                stock_map[product_id] = sum(e.get("stock") or 0 for e in entries)
            if len(rows) < 1000:
                break
            offset += 1000
        return {"map": stock_map}

    result = await _cached(f"stock_by_store:{store_id}", token, loader)
    return result["map"]


@app.get("/api/products")
async def search_products(
    q: str = Query("", alias="q"),
    store_id: str | None = Query(None),
    token: str = Depends(get_current_token),
):
    """Mahsulotlarni nomi, kodi yoki artikuli bo'yicha qidiradi.

    MUHIM: MoySklad'ga har safar to'g'ridan-to'g'ri so'rov yubormaydi — butun
    tovar katalogi xotirada saqlanadi (catalog_cache.py, 1 soatga, MoySklad
    hisobi bo'yicha ajratilgan) va qidiruv shu xotiradagi ro'yxat ustida
    amalga oshiriladi. Bu qidiruvni deyarli oniy qiladi — avval har bir
    qidiruv so'rovi ("name~q"/"code~q"/"article~q" uchun 3 ta alohida MoySklad
    so'rovi) sezilarli sekinlikning asosiy sababi edi.
    """
    account_id, default_price_type_id, stock_by_store = await asyncio.gather(
        _get_account_id(token),
        _get_default_price_type_id(token),
        _get_stock_by_store(token, store_id) if store_id else _no_stock(),
    )
    await catalog_cache.ensure_fresh(account_id, token)
    rows = catalog_cache.search_assortment(account_id, q)[:50]
    return {
        "items": [
            _assortment_row_to_item(r, default_price_type_id, store_id, stock_by_store) for r in rows
        ]
    }


@app.get("/api/products/scan")
async def scan_product(
    code: str = Query(..., min_length=1),
    store_id: str | None = Query(None),
    token: str = Depends(get_current_token),
):
    """Kamera bilan o'qilgan barkod bo'yicha ANIQ moslikni qidiradi — xotiradagi
    keshlangan katalogdan (search_products'dagi kabi sabab)."""
    account_id, default_price_type_id, stock_by_store = await asyncio.gather(
        _get_account_id(token),
        _get_default_price_type_id(token),
        _get_stock_by_store(token, store_id) if store_id else _no_stock(),
    )
    await catalog_cache.ensure_fresh(account_id, token)
    row = catalog_cache.find_by_barcode(account_id, code)
    if row is None:
        return {"item": None}
    return {"item": _assortment_row_to_item(row, default_price_type_id, store_id, stock_by_store)}


@app.get("/api/counterparties")
async def search_counterparties(q: str = Query("", alias="q"), token: str = Depends(get_current_token)):
    """Mijozlarni qidiradi — xotiradagi keshlangan ro'yxatdan (catalog_cache.py),
    tovar qidiruvidagi bilan bir xil sababga ko'ra (MoySklad'ga har safar
    to'g'ridan-to'g'ri urilish o'rniga)."""
    account_id = await _get_account_id(token)
    await catalog_cache.ensure_fresh(account_id, token)
    rows = catalog_cache.search_counterparties(account_id, q)[:20]
    return {
        "items": [{"id": row["id"], "meta": row["meta"], "name": row["name"]} for row in rows]
    }


@app.post("/api/counterparties")
async def create_counterparty(payload: CounterpartyCreate, token: str = Depends(get_current_token)):
    """Yangi mijoz (kontakt) yaratadi — real API'da tekshirilgan, faqat 'name' majburiy."""
    body = {"name": payload.name}
    if payload.phone:
        body["phone"] = payload.phone
    row = await ms_request("POST", "/entity/counterparty", token=token, json=body)
    # Darhol keshga ham qo'shamiz — aks holda shu mijozni (hatto o'zi yaratgan
    # kassir ham) keyingi soat davomida qidirib topa olmas edi.
    account_id = await _get_account_id(token)
    catalog_cache.add_counterparty(account_id, row)
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
        # Do'kon rejimida faqat aniq belgilangan shotlar ko'rsatiladi —
        # kassirni kerak bo'lmagan (masalan boshqa tashkilotlarga tegishli)
        # shotlar bilan chalg'itmaslik uchun (nom bo'yicha aniq moslik).
        if SHOP_ALLOWED_ACCOUNT_NAMES:
            allowed = set(SHOP_ALLOWED_ACCOUNT_NAMES)
            items = [i for i in items if i["name"] in allowed]
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


@app.get("/api/shop/settings")
async def get_shop_settings(_: dict = Depends(get_current_session)):
    """Do'kon rejimi uchun standart sozlamalar — hozircha faqat standart
    dollar kursi (kassir savat ekranida xohlaganda o'zgartirishi mumkin,
    bu shunchaki boshlang'ich qiymat)."""
    return {"default_exchange_rate": DEFAULT_EXCHANGE_RATE}


# ---------------------------------------------------------------------------
# Checkout: Заказ покупателя -> Otgruzka -> To'lov
# ---------------------------------------------------------------------------


def _order_uses_queue(payload: CheckoutRequest) -> bool:
    """Navbatga qo'yish FAQAT bitta MoySklad tashkiloti (EXPECTED_MS_ORGANIZATION_ID)
    uchun sozlangan — boshqa har qanday login (yoki hali CHECKOUT_MODE=queue
    qilib yoqilmagan bo'lsa) hozirgidek to'g'ridan-to'g'ri MoySklad'ga yozadi."""
    if CHECKOUT_MODE != "queue" or not EXPECTED_MS_ORGANIZATION_ID:
        return False
    org_id = _id_from_href((payload.organization_meta or {}).get("href", ""))
    return org_id == EXPECTED_MS_ORGANIZATION_ID


def _items_summary(items) -> str:
    return "; ".join(f"{item.name or item.id or 'Tovar'} x{item.quantity:g}" for item in items)


def _items_total(items) -> float:
    return sum(item.price * item.quantity for item in items)


def _stock_cache_items(items) -> list[tuple[str, float]]:
    return [
        (item.id or _id_from_href(item.assortment_meta.get("href", "")), item.quantity) for item in items
    ]


@app.post("/api/checkout")
async def checkout(payload: CheckoutRequest, session: dict = Depends(get_current_session)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    if SHOP_ORGANIZATION_ID:
        # Do'kon rejimi: checkout hech qachon MoySklad'ga to'g'ridan-to'g'ri
        # yozmaydi — har doim navbatga qo'yiladi. Tashkilot/loyiha/kontragent
        # bu yerda kerak emas (frontend endi ularni yubormaydi) — ular
        # shop_sync.py tomonidan kunlik birlashtirish paytida config'dagi
        # SHOP_ORGANIZATION_ID/SHOP_PROJECT_NAME/SHOP_AGENT_NAME'dan hal
        # qilinadi. "business_day" — Toshkent vaqti bo'yicha 16:50 chegarasi
        # asosida hisoblanadi (shop_day.py).
        order_id = str(uuid4())
        store_id = _id_from_href((payload.store_meta or {}).get("href", ""))
        business_day = business_day_key(now_in_shop_tz())

        await sheets_client.append_pending_order(
            order_id=order_id,
            cashier_name=session["employee_name"],
            store_id=store_id,
            store_name=payload.store_name,
            agent_name=payload.agent_name,
            items_summary=_items_summary(payload.items),
            total_sum=_items_total(payload.items),
            currency_name=payload.currency_name,
            is_debt=payload.is_debt,
            payload_json=payload.model_dump_json(),
            business_day=business_day,
        )
        stock_cache.apply_order(order_id, store_id, _stock_cache_items(payload.items))

        return {
            "mode": "queue",
            "order_id": order_id,
            "status": sheets_client.STATUS_PENDING,
            "queued_at": sheets_client.now_iso(),
        }

    if not _order_uses_queue(payload):
        result = await execute_checkout_chain(payload, session["token"])
        return {"mode": "direct", **result}

    order_id = str(uuid4())
    store_id = _id_from_href((payload.store_meta or {}).get("href", ""))

    await sheets_client.append_pending_order(
        order_id=order_id,
        cashier_name=session["employee_name"],
        store_id=store_id,
        store_name=payload.store_name,
        agent_name=payload.agent_name,
        items_summary=_items_summary(payload.items),
        total_sum=_items_total(payload.items),
        currency_name=payload.currency_name,
        is_debt=payload.is_debt,
        payload_json=payload.model_dump_json(),
    )
    stock_cache.apply_order(order_id, store_id, _stock_cache_items(payload.items))

    return {
        "mode": "queue",
        "order_id": order_id,
        "status": sheets_client.STATUS_PENDING,
        "queued_at": sheets_client.now_iso(),
    }


# ---------------------------------------------------------------------------
# Navbatdagi (hali sinxronlanmagan) buyurtmalarni tahrirlash/bekor qilish
# ---------------------------------------------------------------------------


@app.patch("/api/orders/pending/{order_id}")
async def edit_pending_order(order_id: str, payload: PendingOrderItemsEdit, session: dict = Depends(get_current_session)):
    row = await sheets_client.get_row(order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if row["status"] not in sheets_client.EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Bu buyurtma allaqachon sinxronlashga yuborilgan yoki sinxronlangan — tahrirlab bo'lmaydi",
        )

    try:
        payload_dict = json.loads(row["payload_json"])
    except ValueError:
        raise HTTPException(status_code=500, detail="Buyurtma ma'lumotini o'qib bo'lmadi")
    payload_dict["items"] = [item.model_dump() for item in payload.items]
    new_payload = CheckoutRequest.model_validate(payload_dict)

    # Avval Sheets'ga yozamiz (ishonchli manba) — faqat u muvaffaqiyatli
    # bo'lsa xotiradagi qoldiq keshini yangilaymiz, aks holda ular
    # bir-biridan uzilib qolishi mumkin edi.
    ok = await sheets_client.compare_and_set_status(
        order_id,
        sheets_client.EDITABLE_STATUSES,
        row["status"],
        edited_at=sheets_client.now_iso(),
        items_summary=_items_summary(new_payload.items),
        total_sum=f"{_items_total(new_payload.items):.2f}",
        payload_json=new_payload.model_dump_json(),
    )
    if not ok:
        raise HTTPException(
            status_code=409, detail="Buyurtma shu payt sinxronlashga yuborildi — tahrirlab bo'lmadi"
        )

    stock_cache.release_order(order_id)
    stock_cache.apply_order(order_id, row.get("store_id") or None, _stock_cache_items(new_payload.items))

    return {"order_id": order_id, "status": row["status"]}


@app.delete("/api/orders/pending/{order_id}")
async def delete_pending_order(order_id: str, session: dict = Depends(get_current_session)):
    row = await sheets_client.get_row(order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Buyurtma topilmadi")
    if row["status"] not in sheets_client.EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Bu buyurtma allaqachon sinxronlashga yuborilgan yoki sinxronlangan — o'chirib bo'lmaydi",
        )

    ok = await sheets_client.compare_and_set_status(
        order_id, sheets_client.EDITABLE_STATUSES, sheets_client.STATUS_CANCELLED, edited_at=sheets_client.now_iso()
    )
    if not ok:
        raise HTTPException(
            status_code=409, detail="Buyurtma shu payt sinxronlashga yuborildi — o'chirib bo'lmadi"
        )

    stock_cache.release_order(order_id)
    return {"order_id": order_id, "status": sheets_client.STATUS_CANCELLED}


# ---------------------------------------------------------------------------
# Google Apps Script trigger shu yerni chaqiradi — navbatdagi buyurtmalarni
# MoySklad'ga ko'chiradi. Do'kon rejimida (SHOP_ORGANIZATION_ID sozlangan)
# kuniga BIR MARTA, 16:55 Toshkent vaqtida (shop_sync.py — kunlik
# birlashtirish); aks holda hozirgidek 4 marta/kunda (sync_job.py).
# ---------------------------------------------------------------------------


@app.post("/api/sync/run")
async def sync_run(_: None = Depends(require_sync_secret)):
    if SHOP_ORGANIZATION_ID:
        return await shop_sync.run_shop_sync()
    return await sync_job.run_sync()


@app.get("/api/shop/test-diagnose")
async def shop_test_diagnose(_: None = Depends(require_sync_secret)):
    """VAQTINCHALIK: DailyOrders holati va MoySklad'da bugungi kunlik zakaz
    haqiqatda yaratilganmi-yo'qmi tekshiradi (sinovdan so'ng olib tashlanadi)."""
    from .shop_day import business_day_key, now_in_shop_tz

    token = await sync_job.get_shared_admin_token()
    business_day = business_day_key(now_in_shop_tz())
    daily = await sheets_client.get_daily_order(business_day)
    orders = await ms_request(
        "GET", "/entity/customerorder", token=token,
        params={"filter": f"description~Do'kon kunlik zakaz", "limit": 10, "order": "moment,desc"},
    )
    order_summaries = []
    for o in orders.get("rows", []):
        full_order = await ms_request("GET", f"/entity/customerorder/{o['id']}", token=token)
        order_summaries.append({
            "id": o["id"], "name": o.get("name"), "moment": o.get("moment"),
            "description": o.get("description"),
            "sum": full_order.get("sum"),
            "shipped_sum": full_order.get("shippedSum"),
            "payed_sum": full_order.get("payedSum"),
        })
    return {
        "business_day": business_day,
        "daily_order_row": daily,
        "matching_ms_orders": order_summaries,
    }


@app.post("/api/shop/test-complete")
async def shop_test_complete(ms_order_id: str = Query(...), _: None = Depends(require_sync_secret)):
    """VAQTINCHALIK: MoySklad'da zakaz+otgruzka ikkalasi ham haqiqatda
    yaratilgan (shippedSum orqali tasdiqlangan), lekin javob vaqt tugashi
    bilan uzilgani sabab DailyOrders "needs_manual_check"da qolgan holatni
    qo'lda "synced"ga o'tkazadi — shunda qatorlarning o'z to'lovlari
    tekshirilishi davom etishi mumkin (sinovdan so'ng olib tashlanadi)."""
    from .shop_day import business_day_key, now_in_shop_tz

    token = await sync_job.get_shared_admin_token()
    order = await ms_request("GET", f"/entity/customerorder/{ms_order_id}", token=token)
    demands = await ms_request(
        "GET", "/entity/demand", token=token, params={"limit": 5, "order": "moment,desc"}
    )
    demand = demands["rows"][0]

    business_day = business_day_key(now_in_shop_tz())
    await sheets_client.update_daily_order(
        business_day,
        status=sheets_client.DAILY_STATUS_SYNCED,
        ms_order_id=order["id"], ms_order_name=order.get("name") or "",
        ms_demand_id=demand["id"], ms_demand_name=demand.get("name") or "",
    )
    return {
        "business_day": business_day,
        "order_name": order.get("name"),
        "demand_name": demand.get("name"),
        "demand_sum": demand.get("sum"),
    }


@app.post("/api/shop/test-reset")
async def shop_test_reset(
    ms_order_id: str = Query(None),
    cancel_pending_rows: bool = Query(False),
    _: None = Depends(require_sync_secret),
):
    """VAQTINCHALIK: sinov paytida "osilib qolgan" (orphaned) zakazni o'chirib,
    DailyOrders qatorini qaytadan "pending" holatiga tushiradi, keyingi sync
    urinishi toza boshlansin uchun (sinovdan so'ng olib tashlanadi).
    cancel_pending_rows=true bo'lsa, bugungi ish kunidagi eski (masalan buzilgan
    payload_json'li) sinov qatorlarini ham "cancelled" qilib belgilaydi."""
    from .shop_day import business_day_key, now_in_shop_tz

    token = await sync_job.get_shared_admin_token()
    deleted = None
    if ms_order_id:
        await ms_request("DELETE", f"/entity/customerorder/{ms_order_id}", token=token)
        deleted = ms_order_id

    business_day = business_day_key(now_in_shop_tz())

    cancelled_rows = []
    if cancel_pending_rows:
        all_rows = await sheets_client.get_all_rows()
        for row in all_rows:
            if row.get("business_day") == business_day and row["status"] in (
                sheets_client.STATUS_PENDING, sheets_client.STATUS_FAILED
            ):
                await sheets_client.update_row(row["order_id"], status=sheets_client.STATUS_CANCELLED)
                cancelled_rows.append(row["order_id"])

    await sheets_client.update_daily_order(
        business_day,
        status=sheets_client.DAILY_STATUS_PENDING,
        chain_started_at="",
        last_error="",
        ms_order_id="", ms_order_name="", ms_demand_id="", ms_demand_name="",
    )
    return {
        "deleted_ms_order_id": deleted,
        "business_day": business_day,
        "cancelled_rows": cancelled_rows,
        "reset": True,
    }


# ---------------------------------------------------------------------------
# VAQTINCHALIK: shop_sync.py'ni "Test" kontragenti bilan tekshirish uchun —
# kassir sessiyasiz, faqat sync maxfiy kaliti bilan ishlaydi. Sinovdan so'ng
# OLIB TASHLANADI.
# ---------------------------------------------------------------------------
@app.post("/api/shop/test-seed")
async def shop_test_seed(_: None = Depends(require_sync_secret)):
    token = await sync_job.get_shared_admin_token()
    stores_data = await ms_request("GET", "/entity/store", token=token, params={"limit": 1})
    store = stores_data["rows"][0]

    # Otgruzka uchun tanlangan ombordagi HAQIQIY qoldig'i bor tovar kerak —
    # tasodifiy birinchi tovarda qoldiq bo'lmasligi mumkin (real sinovda
    # aynan shu sabab bilan otgruzka rad etilgan edi).
    store_href = f"{MOYSKLAD_BASE_URL}/entity/store/{store['id']}"
    stock_data = await ms_request(
        "GET", "/report/stock/bystore", token=token,
        params={"filter": f"store={store_href}", "limit": 100},
    )
    in_stock_row = next(
        (r for r in stock_data["rows"] if any((e.get("stock") or 0) > 0 for e in r.get("stockByStore", []))),
        None,
    )
    if not in_stock_row:
        return {"error": f"'{store.get('name')}' omborida qoldig'i bor tovar topilmadi"}
    product = await ms_request("GET", in_stock_row["meta"]["href"], token=token)

    accounts_data = await ms_request(
        "GET", f"/entity/organization/{SHOP_ORGANIZATION_ID}/accounts", token=token, params={"limit": 100}
    )
    som_account = next(
        (a for a in accounts_data["rows"] if (a.get("bankName") or a.get("accountNumber")) == "Do'kon naxt so'm"),
        None,
    )
    usd_account = next(
        (a for a in accounts_data["rows"] if (a.get("bankName") or a.get("accountNumber")) == "Do'kon naxt $"),
        None,
    )
    if not som_account or not usd_account:
        return {"error": "Do'kon naxt so'm / Do'kon naxt $ shotlari topilmadi", "found": [
            a.get("bankName") or a.get("accountNumber") for a in accounts_data["rows"]
        ]}

    currencies_data = await ms_request("GET", "/entity/currency", token=token, params={"limit": 100})
    usd_currency = next(
        (c for c in currencies_data["rows"] if (c.get("isoCode") or "").upper() == "USD"), None
    )
    if not usd_currency:
        return {"error": "USD valyutasi topilmadi"}

    business_day = business_day_key(now_in_shop_tz())
    seeded = []

    # 1) Oddiy so'm sotuv, "Do'kon naxt so'm" orqali to'langan.
    payload1 = CheckoutRequest(
        store_meta=store["meta"],
        items=[{"assortment_meta": product["meta"], "quantity": 1, "price": 1000, "id": product["id"], "name": product.get("name")}],
        is_debt=False,
        account_meta=som_account["meta"],
        document_type="cashin",
    )
    order_id1 = str(uuid4())
    await sheets_client.append_pending_order(
        order_id=order_id1, cashier_name="test-seed", store_id=None, store_name=None,
        agent_name="Test", items_summary=f"{product.get('name')} x1", total_sum=1000,
        currency_name=None, is_debt=False, payload_json=payload1.model_dump_json(),
        business_day=business_day,
    )
    seeded.append(order_id1)

    # 2) $ naqd + so'm qaytim oqimi: tovar $0.17 (2000 so'm ekvivalenti, kurs
    # 12000), mijoz $1 beradi, 1000 so'm qaytim oladi.
    payload2 = CheckoutRequest(
        store_meta=store["meta"],
        items=[{"assortment_meta": product["meta"], "quantity": 1, "price": 0.17, "id": product["id"], "name": product.get("name")}],
        is_debt=False,
        account_meta=usd_account["meta"],
        document_type="cashin",
        currency_meta=usd_currency["meta"],
        exchange_rate=12000,
        cash_given_amount=1,
        cash_change_som=1000,
    )
    order_id2 = str(uuid4())
    await sheets_client.append_pending_order(
        order_id=order_id2, cashier_name="test-seed", store_id=None, store_name=None,
        agent_name="Test", items_summary=f"{product.get('name')} x1 ($ + qaytim)", total_sum=2000,
        currency_name=None, is_debt=False, payload_json=payload2.model_dump_json(),
        business_day=business_day,
    )
    seeded.append(order_id2)

    return {"seeded_order_ids": seeded, "business_day": business_day, "product": product.get("name")}


# ---------------------------------------------------------------------------
# Buyurtmalar tarixi — faqat shu ilova orqali kiritilganlar
# ---------------------------------------------------------------------------


def _history_sort_key(moment: "str | None") -> str:
    # MoySklad "YYYY-MM-DD HH:MM:SS" beradi, Sheets'dagi vaqtlarimiz esa ISO8601
    # "YYYY-MM-DDTHH:MM:SSZ" — ikkalasi ham to'g'ri leksikografik tartiblansin
    # uchun ajratuvchini bir xillashtiramiz.
    return (moment or "").replace(" ", "T")


@app.get("/api/orders/history")
async def get_orders_history(token: str = Depends(get_current_token)):
    """Shu mini ilova orqali yaratilgan buyurtmalar tarixini qaytaradi:
    MoySklad'ga allaqachon sinxronlangan buyurtmalar (maxsus "POS Mini App"
    sotuv kanali bo'yicha filtrlangan) + "queue" rejimida hali navbatda
    turgan (kutilmoqda/xato) buyurtmalar, bitta vaqt bo'yicha tartiblangan
    ro'yxat sifatida."""

    history_token = token
    if MS_SYNC_LOGIN and MS_SYNC_PASSWORD:
        # "queue" rejimida BARCHA sinxronlangan hujjatlar umumiy sync hisobi
        # (MS_SYNC_LOGIN) nomidan yaratiladi — agar KO'RUVCHI kassirning o'z
        # MoySklad huquqi "faqat o'zi yaratgan hujjatlarni ko'rish" bilan
        # cheklangan bo'lsa, o'z tokeni bilan so'ralganda bu hujjatlarning
        # HECH birini ko'rmaydi (garchi aslida aynan o'zi kiritgan bo'lsa
        # ham — chunki MoySklad'ning nazarida ularni sync hisobi yaratgan).
        # Shu sabab Tarix har doim shu umumiy hisob nomidan so'raladi — "Tarix
        # barcha kassirlarga umumiy" tamoyiliga mos, MoySklad'dagi huquq
        # cheklovlaridan qat'i nazar (real hisobda shunday muammo tasdiqlangan).
        #
        # MUHIM: bu — sync_job.py'dagi BILAN ULASHILGAN, keshlangan token
        # (har safar yangisini OLMAYDI) — aks holda "Tarix" har ochilganda
        # HAM, sync ishi HAM mustaqil yangi token olib, MoySklad bir login
        # uchun faqat bitta faol token saqlagani sabab, aynan shu umumiy
        # hisob bilan ilovada FAOL ishlayotgan boshqa foydalanuvchining
        # sessiyasi kutilmaganda uzilib qolar edi (real productionda
        # tasdiqlangan muammo — ikkalasi alohida keshlanganda ham hali
        # bir-birini bekor qilib turardi).
        using_shared_token = True
        try:
            history_token = await sync_job.get_shared_admin_token()
        except HTTPException:
            history_token = token  # sync hisobi ishlamasa ham, hech bo'lmasa o'z ko'rinishi ko'rsatiladi
            using_shared_token = False
    else:
        using_shared_token = False

    async def _fetch(tok: str) -> dict:
        channel_meta = await _get_pos_sales_channel_meta(tok)
        return await ms_request(
            "GET",
            "/entity/customerorder",
            token=tok,
            params={
                "filter": f"salesChannel={channel_meta['href']}",
                "expand": "agent",
                "order": "moment,desc",
                "limit": 50,
            },
        )

    try:
        data = await _fetch(history_token)
    except HTTPException as exc:
        if not using_shared_token or exc.status_code not in (401, 403):
            raise
        # Umumiy token boshqa joyda (masalan katalog isitish yoki sync ishi
        # tomonidan) bekor qilingan bo'lishi mumkin — bir marta majburiy
        # yangilab qayta uriniladi (sync_job.py'dagi bilan bir xil naqsh).
        logger.warning("Tarix uchun umumiy token bekor qilingan (401/403) — majburiy yangilab qayta urinilmoqda")
        history_token = await sync_job.get_shared_admin_token(force_refresh=True)
        data = await _fetch(history_token)

    items = []
    for row in data.get("rows", []):
        total_sum = (row.get("sum") or 0) / 100
        payed_sum = (row.get("payedSum") or 0) / 100
        agent = row.get("agent") or {}
        rate = row.get("rate") or {}
        currency = rate.get("currency") or {}
        currency_href = (currency.get("meta") or {}).get("href")
        items.append(
            {
                "id": row["id"],
                "name": row.get("name"),
                "moment": row.get("moment"),
                "agent_name": agent.get("name"),
                "sum": total_sum,
                "is_paid": total_sum > 0 and payed_sum >= total_sum,
                "currency_id": _id_from_href(currency_href) if currency_href else None,
                "comment": row.get("description"),
                "status": sheets_client.STATUS_SYNCED,
            }
        )

    if CHECKOUT_MODE == "queue" and EXPECTED_MS_ORGANIZATION_ID:
        pending_rows = await sheets_client.get_visible_pending_rows()
        for r in pending_rows:
            try:
                r_payload = json.loads(r["payload_json"] or "{}")
            except ValueError:
                r_payload = {}
            items.append(
                {
                    "id": r["order_id"],
                    "name": None,
                    "moment": r.get("created_at"),
                    "agent_name": r.get("agent_name") or r_payload.get("agent_name"),
                    "sum": float(r.get("total_sum") or 0),
                    "is_paid": False,
                    "currency_id": None,
                    "currency_name": r.get("currency_name") or None,
                    "comment": r_payload.get("comment"),
                    "status": r["status"],
                    "last_error": r.get("last_error") or None,
                    "items_summary": r.get("items_summary"),
                    "items": r_payload.get("items", []),
                }
            )

    items.sort(key=lambda it: _history_sort_key(it.get("moment")), reverse=True)
    return {"items": items}


# Frontend'ni backend bilan bitta origin'dan xizmat ko'rsatish — PWA va CORS uchun qulay.
# Bu mount har doim eng oxirida bo'lishi kerak, aks holda yuqoridagi /api/* marshrutlarni bosib qo'yadi.
_frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
