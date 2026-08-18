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

from . import crypto, sheets_client, stock_cache, sync_job
from .auth import create_session, delete_session, get_current_session, get_current_token, require_sync_secret
from .bot import start_bot, stop_bot
from .cache import _cached
from .checkout_chain import _get_default_currency_id, _get_pos_sales_channel_meta, execute_checkout_chain
from .config import (
    CHECKOUT_MODE,
    EXPECTED_MS_ORGANIZATION_ID,
    GOOGLE_SHEETS_SPREADSHEET_ID,
    MOYSKLAD_BASE_URL,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_HOURS,
)
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


# ---------------------------------------------------------------------------
# Autentifikatsiya
# ---------------------------------------------------------------------------


@app.post("/api/login")
async def login(payload: LoginRequest, response: Response):
    token = await exchange_credentials_for_token(payload.login, payload.password)
    employee = await ms_request("GET", "/context/employee", token=token)
    employee_name = employee.get("name") or employee.get("fullName") or payload.login

    # "queue" rejimida navbatga qo'yilgan buyurtmani soatlab keyin AYNAN shu
    # kassir nomidan sinxronlash uchun parol shifrlab saqlanadi (crypto.py).
    # CREDENTIAL_ENCRYPTION_KEY sozlanmagan bo'lsa (bu funksiya ishlatilmasa),
    # jim o'tkazib yuboriladi — login o'zi baribir davom etadi.
    password_enc = None
    try:
        password_enc = crypto.encrypt_password(payload.password)
    except crypto.CredentialEncryptionNotConfigured:
        pass

    session_id = create_session(token, employee_name, payload.login, password_enc)
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


def _assortment_row_to_item(row: dict, default_price_type_id: "str | None", store_id: "str | None" = None) -> dict:
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

    # Faqat qidiruvga "store_id" berilganda keladi (pastga qarang) — aks
    # holda MoySklad "stock" maydonini butunlay qaytarmaydi. Bor bo'lsa,
    # Google Sheets navbatida hali MoySklad'ga sinxronlanmagan buyurtmalar
    # qoldirgan "deduksiya"ni ham shu yerda ayiramiz — aks holda navbatga
    # qo'yilgan, lekin hali MoySklad'ga yetib bormagan sotuv oxirgi donani
    # ikkinchi mijozga ham sotib qo'yishi mumkin edi.
    stock = row.get("stock")
    if stock is not None and store_id:
        stock = max(0.0, stock - stock_cache.get_deduction(store_id, row.get("id")))

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
    }


def _stock_params(store_id: "str | None") -> dict:
    """"store_id" berilganda MoySklad javobiga o'sha ombordagi ANIQ qoldiq
    ("stock") maydonini qo'shib beradi (real API'da tekshirilgan:
    "entity/assortment"ga "stockStore=<ombor href>" berilsa, har bir tovar
    aynan shu ombor bo'yicha qoldig'i bilan qaytadi — umumiy/boshqa
    omborlardagi qoldiq bilan aralashib ketmaydi)."""
    if not store_id:
        return {}
    return {"stockStore": f"{MOYSKLAD_BASE_URL}/entity/store/{store_id}"}


@app.get("/api/products")
async def search_products(
    q: str = Query("", alias="q"),
    store_id: str | None = Query(None),
    token: str = Depends(get_current_token),
):
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
    stock_params = _stock_params(store_id)

    if not q:
        data = await ms_request(
            "GET",
            "/entity/assortment",
            token=token,
            params={"limit": 50, "expand": "images", **stock_params},
        )
        return {
            "items": [
                _assortment_row_to_item(r, default_price_type_id, store_id) for r in data.get("rows", [])
            ]
        }

    results = await asyncio.gather(
        *[
            ms_request(
                "GET",
                "/entity/assortment",
                token=token,
                params={"filter": f"{field}~{q}", "limit": 50, "expand": "images", **stock_params},
            )
            for field in ("name", "code", "article")
        ]
    )

    merged: dict[str, dict] = {}
    for data in results:
        for row in data.get("rows", []):
            merged[row["id"]] = row

    return {
        "items": [
            _assortment_row_to_item(r, default_price_type_id, store_id) for r in merged.values()
        ]
    }


@app.get("/api/products/scan")
async def scan_product(
    code: str = Query(..., min_length=1),
    store_id: str | None = Query(None),
    token: str = Depends(get_current_token),
):
    """Kamera bilan o'qilgan barkod bo'yicha ANIQ moslikni qidiradi.

    Real API'da tekshirilgan: "filter=barcode=<qiymat>" barkodlar massivi
    ("barcodes": [{"ean13": "..."}]) ichidan aniq moslikni topadi.
    """
    data = await ms_request(
        "GET",
        "/entity/assortment",
        token=token,
        params={"filter": f"barcode={code}", "limit": 1, "expand": "images", **_stock_params(store_id)},
    )
    rows = data.get("rows", [])
    if not rows:
        return {"item": None}
    default_price_type_id = await _get_default_price_type_id(token)
    return {"item": _assortment_row_to_item(rows[0], default_price_type_id, store_id)}


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

    if not _order_uses_queue(payload):
        result = await execute_checkout_chain(payload, session["token"])
        return {"mode": "direct", **result}

    if not session.get("password_enc"):
        # "queue" rejimida sync HAR BIR buyurtmani AYNAN shu kassir nomidan
        # yuboradi (umumiy hisob emas) — buning uchun kirishda parol shifrlab
        # saqlanadi (auth.create_session). Agar shu yerda yo'q bo'lsa, demak
        # server CREDENTIAL_ENCRYPTION_KEY'siz ishga tushirilgan — checkout
        # noaniq holatda davom etgandan ko'ra, aniq xato berish yaxshiroq.
        raise HTTPException(
            status_code=500,
            detail="Server sozlamasi to'liq emas (CREDENTIAL_ENCRYPTION_KEY) — administratorga murojaat qiling",
        )

    order_id = str(uuid4())
    store_id = _id_from_href((payload.store_meta or {}).get("href", ""))

    await sheets_client.append_pending_order(
        order_id=order_id,
        cashier_name=session["employee_name"],
        cashier_login=session["login"],
        cashier_password_enc=session["password_enc"],
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
# Google Apps Script trigger shu yerni chaqiradi (00:00/06:00/12:00/18:00,
# Asia/Tashkent) — navbatdagi buyurtmalarni MoySklad'ga ko'chiradi.
# ---------------------------------------------------------------------------


@app.post("/api/sync/run")
async def sync_run(_: None = Depends(require_sync_secret)):
    return await sync_job.run_sync()


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

    channel_meta = await _get_pos_sales_channel_meta(token)
    channel_href = channel_meta["href"]

    data = await ms_request(
        "GET",
        "/entity/customerorder",
        token=token,
        params={
            "filter": f"salesChannel={channel_href}",
            "expand": "agent",
            "order": "moment,desc",
            "limit": 50,
        },
    )

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
