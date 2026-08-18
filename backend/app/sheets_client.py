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
    "last_attempt_at", "last_error", "synced_at",
    "ms_order_id", "ms_order_name", "ms_demand_id", "ms_demand_name",
    "ms_payment_id", "ms_payment_name",
]

STATUS_PENDING = "pending"
STATUS_SYNCING = "syncing"
STATUS_FAILED = "failed"
STATUS_NEEDS_MANUAL_CHECK = "needs_manual_check"
STATUS_SYNCED = "synced"
STATUS_CANCELLED = "cancelled"

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


def _row_to_dict(row: list, row_number: int) -> dict:
    padded = list(row) + [""] * (len(COLUMNS) - len(row))
    d = dict(zip(COLUMNS, padded))
    d["_row_number"] = row_number
    return d


def _dict_to_row(d: dict) -> list:
    out = []
    for col in COLUMNS:
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


def _open_worksheet_sync() -> gspread.Worksheet:
    if not config.GOOGLE_SHEETS_SPREADSHEET_ID:
        raise SheetsNotConfigured("GOOGLE_SHEETS_SPREADSHEET_ID sozlanmagan")
    client = _build_client_sync()
    spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_SPREADSHEET_ID)
    try:
        ws = spreadsheet.worksheet(config.GOOGLE_SHEETS_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(config.GOOGLE_SHEETS_WORKSHEET_NAME, rows=1000, cols=len(COLUMNS))
    values = ws.get_all_values()
    if not values or values[0][: len(COLUMNS)] != COLUMNS:
        ws.update("A1", [COLUMNS])
        # Sxema o'zgarganda (masalan ustun soni kamaysa) eski sarlavha
        # qatoridan uzunroq bo'lgan "qoldiq" katakchalar tozalanmasdan
        # qolib ketmasligi uchun — ular chalkashlik keltirib chiqarishi mumkin.
        old_len = len(values[0]) if values else 0
        if old_len > len(COLUMNS):
            extra_col_letter_start = gspread.utils.rowcol_to_a1(1, len(COLUMNS) + 1).rstrip("1")
            extra_col_letter_end = gspread.utils.rowcol_to_a1(1, old_len).rstrip("1")
            ws.batch_clear([f"{extra_col_letter_start}1:{extra_col_letter_end}1"])
    return ws


async def _get_worksheet() -> gspread.Worksheet:
    global _worksheet
    if _worksheet is None:
        async with _worksheet_lock:
            if _worksheet is None:
                _worksheet = await asyncio.to_thread(_open_worksheet_sync)
    return _worksheet


def _get_all_rows_sync(ws: gspread.Worksheet) -> list[dict]:
    values = ws.get_all_values()
    rows = []
    for i, raw in enumerate(values[1:], start=2):  # 1-qator — sarlavha
        if not any(raw):
            continue
        rows.append(_row_to_dict(raw, i))
    return rows


async def get_all_rows() -> list[dict]:
    ws = await _get_worksheet()
    return await asyncio.to_thread(_get_all_rows_sync, ws)


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
