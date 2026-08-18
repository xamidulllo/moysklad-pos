"""Google Sheets navbatidagi buyurtmalarni MoySklad'ga ko'chiradigan fon
vazifasi. Google Apps Script trigger'i (00:00/06:00/12:00/18:00, Asia/Tashkent)
POST /api/sync/run orqali shu run_sync()'ni chaqiradi (main.py'ga qarang).

XAVFSIZLIK — nega bu oddiy "hamma pending qatorni qayta ishla" emas:
execute_checkout_chain()'ning idempotentlik kaliti yo'q. Agar sync jarayoni
MoySklad hujjatlarini MUVAFFAQIYATLI yaratgandan KEYIN, lekin Sheets'ga
"synced" deb yozishdan OLDIN halok bo'lsa (Render qayta ishga tushishi,
deploy va h.k.), shu qatorni yana qayta urinish MoySklad'da IKKINCHI, haqiqiy
buyurtma/otgruzka/to'lov yaratib qo'yishi mumkin — bu real pul va real ombor
harakati, ikki marta. Shu sabab "chain_started_at" belgisi ishlatiladi:
MoySklad'ga tegishdan OLDIN halok bo'lish (xavfsiz qayta urinish) bilan
tegishdan KEYIN/vaqtida halok bo'lish (natija noaniq, avtomatik qayta
urinish TAQIQLANADI) farqlanadi.
"""
import json
import logging

from fastapi import HTTPException

from . import sheets_client, stock_cache
from .checkout_chain import RollbackFailedError, execute_checkout_chain
from .config import MS_SYNC_LOGIN, MS_SYNC_PASSWORD
from .moysklad_client import exchange_credentials_for_token
from .schemas import CheckoutRequest

logger = logging.getLogger("moysklad_pos.sync_job")

_running = False


def _format_http_error(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e.get("error", e)) for e in errors)
        return json.dumps(detail, ensure_ascii=False)
    return str(detail)


async def _sync_one_row(row: dict, token: str) -> str:
    """Bitta qatorni sinxronlaydi, natija holatini ("synced"/"failed"/
    "needs_manual_check"/"skipped") qaytaradi."""
    order_id = row["order_id"]

    if row["status"] in (sheets_client.STATUS_PENDING, sheets_client.STATUS_FAILED):
        claimed = await sheets_client.compare_and_set_status(
            order_id,
            {sheets_client.STATUS_PENDING, sheets_client.STATUS_FAILED},
            sheets_client.STATUS_SYNCING,
            last_attempt_at=sheets_client.now_iso(),
        )
        if not claimed:
            # Boshqa parallel jarayon (kassir tahriri/o'chirishi yoki
            # to'qnashgan sync urinishi) shu orada qatorni allaqachon o'zgartirdi.
            return "skipped"

    await sheets_client.update_row(order_id, chain_started_at=sheets_client.now_iso())

    try:
        payload = CheckoutRequest.model_validate(json.loads(row["payload_json"]))
    except Exception as exc:  # buzilgan/eskirgan payload_json — qo'lda tekshirish kerak
        logger.exception("Qator %s uchun payload_json noto'g'ri", order_id)
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_NEEDS_MANUAL_CHECK,
            last_error=f"payload_json o'qib bo'lmadi: {exc}",
            last_attempt_at=sheets_client.now_iso(),
        )
        return "needs_manual_check"

    try:
        result = await execute_checkout_chain(payload, token, external_code=order_id)
    except RollbackFailedError as exc:
        logger.error("Qator %s: rollback muvaffaqiyatsiz, qo'lda tekshirish kerak: %s", order_id, exc)
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_NEEDS_MANUAL_CHECK,
            last_error=str(exc),
            last_attempt_at=sheets_client.now_iso(),
        )
        return "needs_manual_check"
    except HTTPException as exc:
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_FAILED,
            last_error=_format_http_error(exc),
            last_attempt_at=sheets_client.now_iso(),
        )
        # Deduksiya SAQLANIB QOLADI — bu hali ham haqiqiy, MoySklad'ga
        # yuborilmagan sotuv, ombor qoldig'ini kamaytirib turishi kerak.
        return "failed"
    except Exception as exc:  # kutilmagan xatolik — MoySklad holati noaniq bo'lishi mumkin
        logger.exception("Qator %s: kutilmagan xatolik", order_id)
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_NEEDS_MANUAL_CHECK,
            last_error=f"Kutilmagan xatolik: {exc}",
            last_attempt_at=sheets_client.now_iso(),
        )
        return "needs_manual_check"

    payment = result.get("payment") or {}
    await sheets_client.update_row(
        order_id,
        status=sheets_client.STATUS_SYNCED,
        synced_at=sheets_client.now_iso(),
        ms_order_id=result["order"]["id"],
        ms_order_name=result["order"].get("name") or "",
        ms_demand_id=result["demand"]["id"],
        ms_demand_name=result["demand"].get("name") or "",
        ms_payment_id=payment.get("id") or "",
        ms_payment_name=payment.get("name") or "",
    )
    stock_cache.release_order(order_id)
    return "synced"


async def _run_sync_inner() -> dict:
    if not MS_SYNC_LOGIN or not MS_SYNC_PASSWORD:
        return {
            "error": "MS_SYNC_LOGIN/MS_SYNC_PASSWORD sozlanmagan",
            "attempted": 0, "synced": 0, "failed": 0, "needs_manual_check": 0, "results": [],
        }

    # Avval token — hech qanday qatorga tegishdan OLDIN. Shu yerda xato
    # chiqsa, hech narsa o'zgartirilmagan, keyingi trigger butun navbatni
    # xavfsiz qayta urinishi mumkin.
    try:
        token = await exchange_credentials_for_token(MS_SYNC_LOGIN, MS_SYNC_PASSWORD)
    except HTTPException as exc:
        logger.error("Sync uchun MoySklad token olinmadi: %s", exc.detail)
        return {
            "error": f"Token olinmadi: {_format_http_error(exc)}",
            "attempted": 0, "synced": 0, "failed": 0, "needs_manual_check": 0, "results": [],
        }

    rows = await sheets_client.get_syncable_rows()
    counts = {"synced": 0, "failed": 0, "needs_manual_check": 0, "skipped": 0}
    results = []
    for row in rows:
        outcome = await _sync_one_row(row, token)
        counts[outcome] = counts.get(outcome, 0) + 1
        results.append({"order_id": row["order_id"], "outcome": outcome})

    return {
        "attempted": len(rows),
        "synced": counts["synced"],
        "failed": counts["failed"],
        "needs_manual_check": counts["needs_manual_check"],
        "skipped": counts["skipped"],
        "results": results,
    }


async def run_sync() -> dict:
    """Bir vaqtda faqat bitta sync ishlaydi — asyncio kooperativ bo'lgani
    uchun `_running` tekshiruvi va o'rnatilishi orasida hech qanday `await`
    yo'q, shuning uchun bu oddiy bayroq xavfsiz ishlaydi (haqiqiy Lock shart
    emas)."""
    global _running
    if _running:
        return {
            "skipped": True,
            "reason": "sync allaqachon ishlamoqda",
            "attempted": 0, "synced": 0, "failed": 0, "needs_manual_check": 0, "results": [],
        }
    _running = True
    try:
        return await _run_sync_inner()
    finally:
        _running = False
