"""Google Sheets bilan ishlash uchun ingichka qatlam — buyurtmalar navbati
shu yerda saqlanadi (checkout darhol MoySklad'ga yozmaydi, avval shu Sheet'ga
qator qo'shiladi, keyin davriy sync uni MoySklad'ga ko'chiradi).

MUHIM: `gspread` sinxron (requests asosidagi) kutubxona. Bu FastAPI ilova
to'liq async va Render'ning bepul tarifida bitta uvicorn worker ishlaydi —
gspread'ni to'g'ridan-to'g'ri "async def" marshrut ichida chaqirish butun
event loop'ni ushlab turardi (shu paytda boshqa kassirning so'rovi ham
kutib turardi). Shuning uchun HAR BIR gspread chaqiruvi
`asyncio.to_thread(...)` orqali amalga oshiriladi.

Qator manzillanishi HAR DOIM `order_id` (UUID) bo'yicha — qator raqami emas,
chunki bir nechta yozuvchi bo'lganda qator raqamlari siljib turishi mumkin.
"""
import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Optional

import gspread
from google.oauth2.credentials import Credentials as OAuthUserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

from . import config

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

COLUMNS = [
    "order_id", "status", "created_at", "edited_at", "cashier_name",
    "store_id", "store_name", "agent_name", "items_summary", "total_sum",
    "currency_name", "is_debt", "payload_json", "chain_started_at",
    "last_attempt_at", "last_error", "synced_at", "business_day",
    "ms_order_id", "ms_order_name", "ms_demand_id", "ms_demand_name",
    "ms_payment_id", "ms_payment_name",
]

STATUS_PENDING = "pending"
STATUS_SYNCING = "syncing"
STATUS_FAILED = "failed"
STATUS_NEEDS_MANUAL_CHECK = "needs_manual_check"
STATUS_SYNCED = "synced"
STATUS_CANCELLED = "cancelled"

# Do'kon rejimi: bir ish kuni uchun bitta umumiy MoySklad zakazi+otgruzkasini
# kuzatib boradigan alohida jadval varag'i ("DailyOrders"). `business_day`
# (shop_day.business_day_key() natijasi, "YYYY-MM-DD") — manzillash kaliti,
# xuddi asosiy jadvaldagi order_id kabi.
DAILY_ORDERS_WORKSHEET_NAME = "DailyOrders"
DAILY_COLUMNS = [
    "business_day", "status", "created_at", "chain_started_at", "last_error",
    "ms_order_id", "ms_order_name", "ms_demand_id", "ms_demand_name",
]

DAILY_STATUS_PENDING = "pending"
DAILY_STATUS_SYNCING = "syncing"
DAILY_STATUS_SYNCED = "synced"
DAILY_STATUS_NEEDS_MANUAL_CHECK = "needs_manual_check"

# Bu holatlardagi qatorlar hali MoySklad'ning o'z qoldig'ida hisobga
# olinmagan — shuning uchun ombor qoldig'idan (stock_cache) ayirilib turishi
# kerak (needs_manual_check ham shu jumladan — u ham hali MoySklad'da
# yaratilgani noaniq).
PENDING_LIKE_STATUSES = {STATUS_PENDING, STATUS_SYNCING, STATUS_FAILED, STATUS_NEEDS_MANUAL_CHECK}

# "Tarix" ekranida ko'rsatiladigan, hali sinxronlanmagan qatorlar.
VISIBLE_IN_HISTORY_STATUSES = {STATUS_PENDING, STATUS_FAILED, STATUS_NEEDS_MANUAL_CHECK}

# Kassir tahrirlashi/o'chirishi mumkin bo'lgan holatlar.
EDITABLE_STATUSES = {STATUS_PENDING, STATUS_FAILED}


class SheetsNotConfigured(Exception):
    """GOOGLE_SHEETS_SPREADSHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON_B64 sozlanmagan."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row: list, row_number: int, columns: list = COLUMNS) -> dict:
    padded = list(row) + [""] * (len(columns) - len(row))
    d = dict(zip(columns, padded))
    d["_row_number"] = row_number
    return d


def _dict_to_row(d: dict, columns: list = COLUMNS) -> list:
    out = []
    for col in columns:
        value = d.get(col)
        if value is None:
            out.append("")
        elif isinstance(value, bool):
            out.append("true" if value else "false")
        else:
            out.append(str(value))
    return out


_worksheet: "gspread.Worksheet | None" = None
_worksheet_lock = asyncio.Lock()
_daily_worksheet: "gspread.Worksheet | None" = None
_daily_worksheet_lock = asyncio.Lock()


def _build_client_sync() -> gspread.Client:
    """Ikki ulanish usulini qo'llab-quvvatlaydi — avval OAuth (shaxsiy hisob,
    ko'p Google Cloud loyihalarida "Organization Policy" tomonidan
    service-account kalitlari butunlay bloklangani uchun tavsiya etiladi),
    aks holda service-account JSON kaliti."""
    if config.GOOGLE_OAUTH_REFRESH_TOKEN and config.GOOGLE_OAUTH_CLIENT_ID and config.GOOGLE_OAUTH_CLIENT_SECRET:
        creds = OAuthUserCredentials(
            token=None,
            refresh_token=config.GOOGLE_OAUTH_REFRESH_TOKEN,
            client_id=config.GOOGLE_OAUTH_CLIENT_ID,
            client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=_SCOPES,
        )
        return gspread.authorize(creds)

    if not config.GOOGLE_SERVICE_ACCOUNT_JSON_B64:
        raise SheetsNotConfigured(
            "Na OAuth (GOOGLE_OAUTH_*), na GOOGLE_SERVICE_ACCOUNT_JSON_B64 sozlanmagan"
        )
    info = json.loads(base64.b64decode(config.GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode("utf-8"))
    creds = ServiceAccountCredentials.from_service_account_info(info, scopes=_SCOPES)
    return gspread.authorize(creds)


def _open_worksheet_sync(worksheet_name: str, columns: list) -> gspread.Worksheet:
    if not config.GOOGLE_SHEETS_SPREADSHEET_ID:
        raise SheetsNotConfigured("GOOGLE_SHEETS_SPREADSHEET_ID sozlanmagan")
    client = _build_client_sync()
    spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_SPREADSHEET_ID)
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(worksheet_name, rows=1000, cols=len(columns))
    values = ws.get_all_values()
    if not values or values[0][: len(columns)] != columns:
        ws.update("A1", [columns])
        # Sxema o'zgarganda (masalan ustun soni kamaysa) eski sarlavha
        # qatoridan uzunroq bo'lgan "qoldiq" katakchalar tozalanmasdan
        # qolib ketmasligi uchun — ular chalkashlik keltirib chiqarishi mumkin.
        old_len = len(values[0]) if values else 0
        if old_len > len(columns):
            extra_col_letter_start = gspread.utils.rowcol_to_a1(1, len(columns) + 1).rstrip("1")
            extra_col_letter_end = gspread.utils.rowcol_to_a1(1, old_len).rstrip("1")
            ws.batch_clear([f"{extra_col_letter_start}1:{extra_col_letter_end}1"])
    return ws


async def _get_worksheet() -> gspread.Worksheet:
    global _worksheet
    if _worksheet is None:
        async with _worksheet_lock:
            if _worksheet is None:
                _worksheet = await asyncio.to_thread(
                    _open_worksheet_sync, config.GOOGLE_SHEETS_WORKSHEET_NAME, COLUMNS
                )
    return _worksheet


async def _get_daily_worksheet() -> gspread.Worksheet:
    global _daily_worksheet
    if _daily_worksheet is None:
        async with _daily_worksheet_lock:
            if _daily_worksheet is None:
                _daily_worksheet = await asyncio.to_thread(
                    _open_worksheet_sync, DAILY_ORDERS_WORKSHEET_NAME, DAILY_COLUMNS
                )
    return _daily_worksheet


def _get_all_rows_sync(ws: gspread.Worksheet, columns: list = COLUMNS) -> list[dict]:
    values = ws.get_all_values()
    rows = []
    for i, raw in enumerate(values[1:], start=2):  # 1-qator — sarlavha
        if not any(raw):
            continue
        rows.append(_row_to_dict(raw, i, columns))
    return rows


async def get_all_rows() -> list[dict]:
    ws = await _get_worksheet()
    return await asyncio.to_thread(_get_all_rows_sync, ws, COLUMNS)


async def get_visible_pending_rows() -> list[dict]:
    rows = await get_all_rows()
    return [r for r in rows if r["status"] in VISIBLE_IN_HISTORY_STATUSES]


async def get_syncable_rows() -> list[dict]:
    """`pending`/`failed` qatorlar, shuningdek `chain_started_at` hali bo'sh
    bo'lgan `syncing` qatorlar (oldingi urinish MoySklad'ga tegmasdan
    to'xtagan — xavfsiz qayta urinish). `chain_started_at` allaqachon
    yozilgan `syncing` qatorlar chetlab o'tiladi — MoySklad'dagi haqiqiy
    holat noaniq, avtomatik qayta urinish xavfli."""
    rows = await get_all_rows()
    result = []
    for r in rows:
        status = r["status"]
        if status in (STATUS_PENDING, STATUS_FAILED):
            result.append(r)
        elif status == STATUS_SYNCING and not r.get("chain_started_at"):
            result.append(r)
    return result


async def get_row(order_id: str) -> Optional[dict]:
    rows = await get_all_rows()
    for r in rows:
        if r["order_id"] == order_id:
            return r
    return None


def _append_row_sync(ws: gspread.Worksheet, row_values: list) -> None:
    ws.append_row(row_values, value_input_option="RAW")


async def append_pending_order(
    order_id: str,
    cashier_name: str,
    store_id: "str | None",
    store_name: "str | None",
    agent_name: "str | None",
    items_summary: str,
    total_sum: float,
    currency_name: "str | None",
    is_debt: bool,
    payload_json: str,
    business_day: "str | None" = None,
) -> None:
    row = {
        "order_id": order_id,
        "status": STATUS_PENDING,
        "created_at": now_iso(),
        "edited_at": "",
        "cashier_name": cashier_name or "",
        "store_id": store_id or "",
        "store_name": store_name or "",
        "agent_name": agent_name or "",
        "items_summary": items_summary,
        "total_sum": f"{total_sum:.2f}",
        "currency_name": currency_name or "",
        "is_debt": is_debt,
        "payload_json": payload_json,
        "business_day": business_day or "",
    }
    ws = await _get_worksheet()
    await asyncio.to_thread(_append_row_sync, ws, _dict_to_row(row))


def _update_row_sync(ws: gspread.Worksheet, row_number: int, updates: dict) -> None:
    current = ws.row_values(row_number)
    current_dict = _row_to_dict(current, row_number)
    for key, value in updates.items():
        current_dict[key] = value
    ws.update(f"A{row_number}", [_dict_to_row(current_dict)], value_input_option="RAW")


async def update_row(order_id: str, **updates) -> bool:
    row = await get_row(order_id)
    if not row:
        return False
    ws = await _get_worksheet()
    await asyncio.to_thread(_update_row_sync, ws, row["_row_number"], updates)
    return True


async def compare_and_set_status(order_id: str, expected_statuses: set, new_status: str, **extra_updates) -> bool:
    """Oddiy "o'qi-keyin-yoz" — gspread'da haqiqiy atomik compare-and-swap yo'q,
    shuning uchun juda kichik poyga (race) oynasi qoladi (ikkita sync ishi
    bir vaqtda ishga tushishi yoki kassir tahriri bilan to'qnashishi mumkin).
    Bu do'konning haqiqiy yuklamasi uchun bu yetarlicha xavfsiz."""
    row = await get_row(order_id)
    if not row or row["status"] not in expected_statuses:
        return False
    updates = {"status": new_status, **extra_updates}
    ws = await _get_worksheet()
    await asyncio.to_thread(_update_row_sync, ws, row["_row_number"], updates)
    return True


async def get_syncable_rows_by_business_day() -> dict:
    """get_syncable_rows() natijasini `business_day` bo'yicha guruhlab
    qaytaradi (Do'kon'ning kunlik birlashtirish sinxronizatsiyasi uchun),
    har bir guruh ichida `created_at` bo'yicha xronologik tartiblangan."""
    rows = await get_syncable_rows()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        day = r.get("business_day") or ""
        if not day:
            continue
        grouped.setdefault(day, []).append(r)
    for day_rows in grouped.values():
        day_rows.sort(key=lambda r: r.get("created_at") or "")
    return grouped


# ---------------------------------------------------------------------------
# "DailyOrders" jadvali — bir ish kuni uchun bitta umumiy MoySklad
# zakazi+otgruzkasini kuzatib boradi. Yuqoridagi bilan bir xil naqsh
# (order_id o'rniga business_day, xuddi shu CAS/xavfsizlik mantig'i).
# ---------------------------------------------------------------------------


def _get_all_daily_rows_sync(ws: gspread.Worksheet) -> list[dict]:
    return _get_all_rows_sync(ws, DAILY_COLUMNS)


async def get_daily_order(business_day: str) -> Optional[dict]:
    ws = await _get_daily_worksheet()
    rows = await asyncio.to_thread(_get_all_daily_rows_sync, ws)
    for r in rows:
        if r["business_day"] == business_day:
            return r
    return None


async def create_daily_order(business_day: str) -> None:
    """Berilgan ish kuni uchun `DailyOrders`da hali qator yo'q bo'lsa,
    `pending` holatida yangi qator qo'shadi. Chaqiruvchi buni faqat
    get_daily_order() None qaytarganda chaqirishi kerak — shu orada boshqa
    parallel jarayon ham qator qo'shib ulgurgan bo'lishi mumkin (kichik poyga
    oynasi, xuddi boshqa CAS funksiyalaridagi kabi qabul qilingan)."""
    row = {
        "business_day": business_day,
        "status": DAILY_STATUS_PENDING,
        "created_at": now_iso(),
        "chain_started_at": "",
        "last_error": "",
        "ms_order_id": "",
        "ms_order_name": "",
        "ms_demand_id": "",
        "ms_demand_name": "",
    }
    ws = await _get_daily_worksheet()
    await asyncio.to_thread(_append_row_sync, ws, _dict_to_row(row, DAILY_COLUMNS))


def _update_daily_row_sync(ws: gspread.Worksheet, row_number: int, updates: dict) -> None:
    current = ws.row_values(row_number)
    current_dict = _row_to_dict(current, row_number, DAILY_COLUMNS)
    for key, value in updates.items():
        current_dict[key] = value
    ws.update(f"A{row_number}", [_dict_to_row(current_dict, DAILY_COLUMNS)], value_input_option="RAW")


async def update_daily_order(business_day: str, **updates) -> bool:
    row = await get_daily_order(business_day)
    if not row:
        return False
    ws = await _get_daily_worksheet()
    await asyncio.to_thread(_update_daily_row_sync, ws, row["_row_number"], updates)
    return True


async def compare_and_set_daily_status(
    business_day: str, expected_statuses: set, new_status: str, **extra_updates
) -> bool:
    row = await get_daily_order(business_day)
    if not row or row["status"] not in expected_statuses:
        return False
    return await update_daily_order(business_day, status=new_status, **extra_updates)


# ---------------------------------------------------------------------------
# Katalog/mijozlar "suratlanmasi" (snapshot) — MoySklad hozir ba'zan juda sekin
# yoki umuman javob bermay qolayotgani sabab (real productionda 60+ soniyalik
# ReadTimeout'lar bilan tasdiqlangan), butun tovar/mijoz ro'yxati bu yerda
# ham saqlanadi. Ilova qayta ishga tushganda (yoki MoySklad hozircha
# ishlamasa) katalog keshi AVVAL shu "eski, lekin mavjud" nusxadan darhol
# to'ldiriladi (hech qachon MoySklad'ni kutmasdan), so'ng fonda haqiqiy
# MoySklad'dan yangilanadi — catalog_cache.py'ga qarang.
# ---------------------------------------------------------------------------
_CATALOG_SNAPSHOT_WORKSHEET_NAME = "CatalogSnapshot"
_CUSTOMERS_SNAPSHOT_WORKSHEET_NAME = "CustomersSnapshot"
_SNAPSHOT_COLUMNS = ["id", "data_json"]

_catalog_snapshot_worksheet: "gspread.Worksheet | None" = None
_catalog_snapshot_lock = asyncio.Lock()
_customers_snapshot_worksheet: "gspread.Worksheet | None" = None
_customers_snapshot_lock = asyncio.Lock()


async def _get_catalog_snapshot_worksheet() -> gspread.Worksheet:
    global _catalog_snapshot_worksheet
    if _catalog_snapshot_worksheet is None:
        async with _catalog_snapshot_lock:
            if _catalog_snapshot_worksheet is None:
                _catalog_snapshot_worksheet = await asyncio.to_thread(
                    _open_worksheet_sync, _CATALOG_SNAPSHOT_WORKSHEET_NAME, _SNAPSHOT_COLUMNS
                )
    return _catalog_snapshot_worksheet


async def _get_customers_snapshot_worksheet() -> gspread.Worksheet:
    global _customers_snapshot_worksheet
    if _customers_snapshot_worksheet is None:
        async with _customers_snapshot_lock:
            if _customers_snapshot_worksheet is None:
                _customers_snapshot_worksheet = await asyncio.to_thread(
                    _open_worksheet_sync, _CUSTOMERS_SNAPSHOT_WORKSHEET_NAME, _SNAPSHOT_COLUMNS
                )
    return _customers_snapshot_worksheet


def _save_snapshot_sync(ws: gspread.Worksheet, rows: list[dict]) -> None:
    values = [_SNAPSHOT_COLUMNS] + [
        [str(r.get("id") or ""), json.dumps(r, ensure_ascii=False)] for r in rows
    ]
    old_row_count = ws.row_count
    ws.update("A1", values, value_input_option="RAW")
    # Yangi ro'yxat eskisidan qisqaroq bo'lsa, ortiqcha eski qatorlarni
    # tozalaymiz — lekin AVVAL yangi ma'lumot yozilgandan KEYIN, shunda
    # yozish o'rtasida uzilib qolsa ham eski (to'liq) nusxa saqlanib qoladi.
    new_row_count = len(values)
    if old_row_count > new_row_count:
        ws.batch_clear([f"A{new_row_count + 1}:B{old_row_count}"])


def _load_snapshot_sync(ws: gspread.Worksheet) -> list[dict]:
    values = ws.get_all_values()
    rows = []
    for raw in values[1:]:
        if len(raw) < 2 or not raw[1]:
            continue
        try:
            rows.append(json.loads(raw[1]))
        except ValueError:
            continue
    return rows


async def save_catalog_snapshot(assortment_rows: list[dict]) -> None:
    ws = await _get_catalog_snapshot_worksheet()
    await asyncio.to_thread(_save_snapshot_sync, ws, assortment_rows)


async def load_catalog_snapshot() -> list[dict]:
    ws = await _get_catalog_snapshot_worksheet()
    return await asyncio.to_thread(_load_snapshot_sync, ws)


async def save_customers_snapshot(counterparty_rows: list[dict]) -> None:
    ws = await _get_customers_snapshot_worksheet()
    await asyncio.to_thread(_save_snapshot_sync, ws, counterparty_rows)


async def load_customers_snapshot() -> list[dict]:
    ws = await _get_customers_snapshot_worksheet()
    return await asyncio.to_thread(_load_snapshot_sync, ws)


# Har bir ombor uchun so'nggi ma'lum qoldiq (astatka) suratlanmasi — bitta
# qator = bitta ombor, "report/stock/bystore" MoySklad'da sekin/ishlamay
# qolganda ham kassir oxirgi ma'lum qoldiq bilan ishlashda davom etishi uchun.
_STOCK_SNAPSHOT_WORKSHEET_NAME = "StockSnapshot"
_STOCK_SNAPSHOT_COLUMNS = ["store_id", "data_json"]
_stock_snapshot_worksheet: "gspread.Worksheet | None" = None
_stock_snapshot_lock = asyncio.Lock()


async def _get_stock_snapshot_worksheet() -> gspread.Worksheet:
    global _stock_snapshot_worksheet
    if _stock_snapshot_worksheet is None:
        async with _stock_snapshot_lock:
            if _stock_snapshot_worksheet is None:
                _stock_snapshot_worksheet = await asyncio.to_thread(
                    _open_worksheet_sync, _STOCK_SNAPSHOT_WORKSHEET_NAME, _STOCK_SNAPSHOT_COLUMNS
                )
    return _stock_snapshot_worksheet


def _save_stock_snapshot_sync(ws: gspread.Worksheet, store_id: str, stock_map: dict) -> None:
    values = ws.get_all_values()
    rows = {r[0]: r[1] for r in values[1:] if len(r) >= 2 and r[0]}
    rows[store_id] = json.dumps(stock_map, ensure_ascii=False)
    new_values = [_STOCK_SNAPSHOT_COLUMNS] + [[sid, data] for sid, data in rows.items()]
    old_row_count = ws.row_count
    ws.update("A1", new_values, value_input_option="RAW")
    new_row_count = len(new_values)
    if old_row_count > new_row_count:
        ws.batch_clear([f"A{new_row_count + 1}:B{old_row_count}"])


def _load_stock_snapshot_sync(ws: gspread.Worksheet, store_id: str) -> "dict[str, float] | None":
    values = ws.get_all_values()
    for raw in values[1:]:
        if len(raw) >= 2 and raw[0] == store_id and raw[1]:
            try:
                return json.loads(raw[1])
            except ValueError:
                return None
    return None


async def save_stock_snapshot(store_id: str, stock_map: "dict[str, float]") -> None:
    ws = await _get_stock_snapshot_worksheet()
    await asyncio.to_thread(_save_stock_snapshot_sync, ws, store_id, stock_map)


async def load_stock_snapshot(store_id: str) -> "dict[str, float] | None":
    ws = await _get_stock_snapshot_worksheet()
    return await asyncio.to_thread(_load_stock_snapshot_sync, ws, store_id)
