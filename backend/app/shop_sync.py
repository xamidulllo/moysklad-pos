"""Do'kon rejimi uchun kunlik birlashtirish sinxronizatsiyasi.

Bir "ish kuni" (shop_day.py — Toshkent vaqti bo'yicha 16:50 chegarasi)
davomidagi BARCHA Google Sheets navbatidagi sotuvlar BITTA MoySklad
zakazi (customerorder) + BITTA otgruzka (demand)ga birlashtiriladi — bu
ikkalasi kuniga FAQAT BIR MARTA yaratiladi (sheets_client.py'dagi
"DailyOrders" jadvalida kuzatiladi). Har bir sotuv esa O'ZINING to'lov
hujjatini (yoki $-naqd+so'm-qaytim holatida IKKITASINI) oladi, shu umumiy
zakazga bog'langan holda.

MUHIM (hali LIVE MoySklad API'da tasdiqlanmagan — productionga chiqarishdan
oldin SINOV loyiha/kontragent bilan tekshirilishi kerak, hech qachon haqiqiy
"Do'kon"ga emas):
  1) bitta zakazga bir nechta to'lov hujjati "operations[].linkedSum" bilan
     qism-qism bog'lanishi kutilganidek ishlashi;
  2) to'lov hujjati o'ziga bog'langan zakazdan FARQLI valyuta/kursda
     bo'lishi mumkinligi (masalan zakaz so'mda, bitta to'lov esa $da) —
     agar MoySklad buni rad etsa, to'lov summasini so'mga o'zimiz
     konvertatsiya qilib yuborish kerak bo'ladi;
  3) "Qaytim" xarajat moddasi bilan zakazga bog'lanmagan chiqim hujjati
     (cashout) kutilganidek ishlashi.

Xavfsizlik naqshi checkout_chain.py/sync_job.py'dagi bilan bir xil:
"chain_started_at" MoySklad'ga TEGISHDAN OLDIN halok bo'lish (xavfsiz qayta
urinish) bilan tegishdan KEYIN/vaqtida halok bo'lishni (natija noaniq,
avtomatik qayta urinish TAQIQLANADI) farqlaydi — bu yerda ikki bosqichda
qo'llaniladi: (a) kunning umumiy zakaz+otgruzkasi uchun (DailyOrders qatori),
(b) har bir sotuvning o'z to'lovi uchun (asosiy Orders qatori, xuddi
sync_job.py'dagi kabi).
"""
import json
import logging

import httpx
from fastapi import HTTPException

from . import sheets_client, stock_cache
from .cache import _cached
from .checkout_chain import (
    RollbackFailedError,
    _get_pos_sales_channel_meta,
    _get_required_order_attributes,
)
from .config import (
    MOYSKLAD_BASE_URL,
    SHOP_AGENT_NAME,
    SHOP_CHANGE_EXPENSE_ARTICLE_NAME,
    SHOP_ORGANIZATION_ID,
    SHOP_PROJECT_NAME,
    SHOP_SOM_ACCOUNT_NAME,
)
from .moysklad_client import ms_request, ms_request_resilient
from .schemas import CheckoutRequest
from .sync_job import get_shared_admin_token
from .utils import _to_minor_units

logger = logging.getLogger("moysklad_pos.shop_sync")

_running = False

# MoySklad ba'zan bitta hujjat yaratish so'roviga standart (25s) vaqtdan
# ko'proq javob berishi real productionda tasdiqlangan — kunlik zakaz/otgruzka
# yaratish fonda ketadi (kassirni kutdirmaydi), shuning uchun bu ikkalasi
# uchun ancha kattaroq timeout ishlatiladi.
_DOC_CREATE_TIMEOUT_SECONDS = 45.0


class DailyOrderAmbiguousError(Exception):
    """Zakaz yoki otgruzka yaratish so'rovi MoySklad'ga yetib borgan-bormagani
    (tarmoq xatosi/vaqt tugashi sabab) ANIQ EMAS — hujjat aslida yaratilgan
    bo'lishi ham mumkin. Bunday holatda avtomatik qayta urinish (dublikat
    yaratib qo'yishi mumkin) VA avtomatik rollback (haqiqatda yaratilgan
    hujjatni noto'g'ri o'chirib yuborishi mumkin) ikkalasi ham xavfli —
    shuning uchun bu alohida xato turi bilan ko'tariladi, chaqiruvchi buni
    "needs_manual_check" deb belgilashi kerak."""


def _org_meta() -> dict:
    return {
        "href": f"{MOYSKLAD_BASE_URL}/entity/organization/{SHOP_ORGANIZATION_ID}",
        "type": "organization",
        "mediaType": "application/json",
    }


async def _get_shop_project_meta(token: str) -> "dict | None":
    async def loader():
        data = await ms_request_resilient("GET", "/entity/project", token=token, params={"limit": 100})
        match = next((r for r in data.get("rows", []) if r.get("name") == SHOP_PROJECT_NAME), None)
        return {"meta": match["meta"] if match else None}

    result = await _cached("shop_project_meta", token, loader)
    return result["meta"]


async def _get_shop_agent_meta(token: str) -> dict:
    async def loader():
        data = await ms_request_resilient(
            "GET", "/entity/counterparty", token=token,
            params={"filter": f"name={SHOP_AGENT_NAME}", "limit": 1},
        )
        rows = data.get("rows", [])
        if not rows:
            raise HTTPException(status_code=500, detail=f"Kontragent topilmadi: {SHOP_AGENT_NAME}")
        return {"meta": rows[0]["meta"]}

    result = await _cached("shop_agent_meta", token, loader)
    return result["meta"]


async def _get_account_meta_by_name(token: str, account_name: str) -> "dict | None":
    """SHOP_SOM_ACCOUNT_NAME kabi aniq nom bo'yicha hisob meta'sini qaytaradi
    (qaytim uchun chiqim hujjatida ishlatiladi)."""

    async def loader():
        data = await ms_request_resilient(
            "GET", f"/entity/organization/{SHOP_ORGANIZATION_ID}/accounts", token=token, params={"limit": 100}
        )
        for row in data.get("rows", []):
            label = row.get("bankName") or row.get("accountNumber") or ""
            if label == account_name:
                return {"meta": row["meta"]}
        return {"meta": None}

    result = await _cached(f"shop_account_meta:{account_name}", token, loader)
    return result["meta"]


async def _get_change_expense_article_meta(token: str) -> "dict | None":
    """"Qaytim" nomli xarajat moddasi (expenseitem) — chiqim hujjatida
    hisobotda aniq ko'rinishi uchun. Topilmasa yaratiladi."""

    async def loader():
        data = await ms_request_resilient("GET", "/entity/expenseitem", token=token, params={"limit": 100})
        existing = next(
            (r for r in data.get("rows", []) if r.get("name") == SHOP_CHANGE_EXPENSE_ARTICLE_NAME), None
        )
        if existing:
            return {"meta": existing["meta"]}
        created = await ms_request(
            "POST", "/entity/expenseitem", token=token, json={"name": SHOP_CHANGE_EXPENSE_ARTICLE_NAME}
        )
        return {"meta": created["meta"]}

    result = await _cached("shop_change_expense_article", token, loader)
    return result["meta"]


def _row_positions(row: dict) -> list:
    """Bitta Sheets qatoridagi payload_json'dan MoySklad "positions" ro'yxatini
    quradi. MUHIM: umumiy kunlik zakaz har doim tashkilotning BAZAVIY
    valyutasida (so'm) yaratiladi — agar shu qator o'zi CHET EL valyutasida
    kiritilgan bo'lsa (payload.currency_meta bор), narx shu qatorning O'Z
    exchange_rate'i bilan so'mga o'giriladi (checkout_chain.py'dagi rate
    yo'nalishi bilan bir xil: exchange_rate = "1 chet el valyutasi = X so'm").
    """
    payload = CheckoutRequest.model_validate(json.loads(row["payload_json"]))
    convert = bool(payload.currency_meta) and payload.exchange_rate and payload.exchange_rate > 0
    positions = []
    for item in payload.items:
        price_in_som = item.price * payload.exchange_rate if convert else item.price
        positions.append(
            {
                "quantity": item.quantity,
                "price": _to_minor_units(price_in_som),
                "assortment": {"meta": item.assortment_meta},
            }
        )
    return positions


async def _create_daily_order_and_demand(business_day: str, rows: list, token: str) -> None:
    """Berilgan ish kunining BARCHA qatorlaridagi tovarlarni xronologik
    tartibda birlashtirib, BITTA zakaz + unga bog'langan BITTA otgruzka
    yaratadi, natijani DailyOrders'ga yozadi. Xatoda avtomatik bekor qilish
    (rollback) — checkout_chain.py'dagi bir xil naqsh."""
    org_meta = _org_meta()
    agent_meta = await _get_shop_agent_meta(token)
    project_meta = await _get_shop_project_meta(token)
    # Do'kon bitta ombor/joylashuvda ishlaydi deb qabul qilingan — kunlik
    # zakaz+otgruzka shu kunning BIRINCHI qatoridagi ombordan olinadi (agar
    # kelajakda bir necha ombor kerak bo'lsa, kunni ombor bo'yicha ham
    # guruhlash kerak bo'ladi).
    store_meta = CheckoutRequest.model_validate(json.loads(rows[0]["payload_json"])).store_meta

    positions: list = []
    for row in rows:
        positions.extend(_row_positions(row))

    order_body = {
        "organization": {"meta": org_meta},
        "agent": {"meta": agent_meta},
        "store": {"meta": store_meta},
        "positions": positions,
        "applicable": True,
        "salesChannel": {"meta": await _get_pos_sales_channel_meta(token)},
        "description": f"Do'kon kunlik zakaz — {business_day}",
    }
    if project_meta:
        order_body["project"] = {"meta": project_meta}
    required_attrs = await _get_required_order_attributes(token)
    if required_attrs:
        order_body["attributes"] = required_attrs

    try:
        order = await ms_request(
            "POST", "/entity/customerorder", token=token, json=order_body, timeout=_DOC_CREATE_TIMEOUT_SECONDS
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # Zakaz haqiqatda yaratilgan yoki yo'qmi — aniq emas (real productionda
        # tasdiqlangan: MoySklad ba'zan hujjatni yaratib ulguradi, lekin javob
        # kelmaydi). Avtomatik qayta urinish DUBLIKAT zakaz yaratib qo'yishi
        # mumkin, shuning uchun bu yerda taqiqlanadi — qo'lda tekshirish kerak.
        raise DailyOrderAmbiguousError(
            f"Kunlik zakaz yaratishda tarmoq xatosi (holat noaniq): {exc}"
        ) from exc

    demand_body = {
        "organization": {"meta": org_meta},
        "agent": {"meta": agent_meta},
        "store": {"meta": store_meta},
        "positions": positions,
        "applicable": True,
        "customerOrder": {"meta": order["meta"]},
    }
    if project_meta:
        demand_body["project"] = {"meta": project_meta}
    try:
        demand = await ms_request(
            "POST", "/entity/demand", token=token, json=demand_body, timeout=_DOC_CREATE_TIMEOUT_SECONDS
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # Otgruzka haqiqatda yaratilgan yoki yo'qmi — aniq emas. Bu holatda
        # zakazni AVTOMATIK o'chirib bo'lmaydi: agar otgruzka aslida
        # yaratilgan bo'lsa, unga bog'langan zakazni o'chirish MoySklad'da
        # "otgruzka bor, zakazi yo'q" kabi buzilgan holat qoldirardi. Shu
        # sabab hech qanday rollback urinilmasdan to'g'ridan-to'g'ri qo'lda
        # tekshirishga yuboriladi (real productionda aynan shu holat
        # kuzatilgan: zakaz #02340 yaratildi, otgruzka so'rovi vaqt tugashi
        # bilan uzildi).
        raise DailyOrderAmbiguousError(
            f"Kunlik otgruzka yaratishda tarmoq xatosi (holat noaniq, zakaz #{order.get('name')}): {exc}"
        ) from exc
    except HTTPException:
        # MoySklad'ning O'ZI aniq rad etdi (tarmoq xatosi emas) — zakaz
        # haqiqatda yaratilgani aniq, endi hech narsaga bog'lanmagan holda
        # qolmasligi uchun xavfsiz orqaga qaytariladi.
        try:
            await ms_request("DELETE", f"/entity/customerorder/{order['id']}", token=token)
        except HTTPException as rollback_err:
            raise RollbackFailedError(
                f"Kunlik otgruzka yaratilmadi va zakaz #{order.get('name')}ni orqaga qaytarib bo'lmadi: {rollback_err.detail}"
            ) from rollback_err
        except (httpx.TimeoutException, httpx.TransportError) as rollback_exc:
            raise DailyOrderAmbiguousError(
                f"Otgruzka rad etildi va zakaz #{order.get('name')}ni orqaga qaytarishda tarmoq xatosi (holat noaniq): {rollback_exc}"
            ) from rollback_exc
        raise

    await sheets_client.update_daily_order(
        business_day,
        status=sheets_client.DAILY_STATUS_SYNCED,
        ms_order_id=order["id"],
        ms_order_name=order.get("name") or "",
        ms_demand_id=demand["id"],
        ms_demand_name=demand.get("name") or "",
    )


async def _sync_row_payment(row: dict, daily_order: dict, token: str) -> str:
    """Bitta sotuvning o'z to'lov hujjatini (yoki $-naqd+so'm-qaytim holatida
    ikkitasini) umumiy kunlik zakazga bog'langan holda yaratadi. sync_job.py
    ning _sync_one_row() bilan bir xil natija turlarini qaytaradi."""
    order_id = row["order_id"]
    try:
        payload = CheckoutRequest.model_validate(json.loads(row["payload_json"]))
    except Exception as exc:
        logger.exception("Qator %s uchun payload_json noto'g'ri", order_id)
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_NEEDS_MANUAL_CHECK,
            last_error=f"payload_json o'qib bo'lmadi: {exc}",
            last_attempt_at=sheets_client.now_iso(),
        )
        return "needs_manual_check"

    order_meta = {
        "href": f"{MOYSKLAD_BASE_URL}/entity/customerorder/{daily_order['ms_order_id']}",
        "type": "customerorder",
        "mediaType": "application/json",
    }
    agent_meta = await _get_shop_agent_meta(token)
    row_sum_minor = _to_minor_units(sum(i.price * i.quantity for i in payload.items))

    try:
        if payload.is_debt:
            payment_ids = {}
        elif payload.cash_given_amount is not None:
            # Chet el valyutasida naqd + so'mda qaytim oqimi (foydalanuvchi
            # bilan tasdiqlangan 4-qaror): TO'LIQ berilgan summa daromad
            # sifatida, zakazga TO'LIQ bog'langan holda yoziladi; qaytim esa
            # ALOHIDA, zakazga bog'lanmagan chiqim hujjati sifatida.
            # MUHIM (real MoySklad API'da tekshirilgan): to'lov hujjati
            # BAZAVIY valyutadagi (so'm) umumiy kunlik zakazga bog'lanadi —
            # MoySklad "rate.value != 1" bilan chet el valyutasidagi to'lovni
            # bazaviy valyutadagi zakazga bog'lashni RAD ETADI (xato 3007,
            # "Нельзя задать курс валюты учета, отличный от 1"). Shu sabab
            # to'lov ham SO'MDA (berilgan summa * exchange_rate) yoziladi —
            # jismoniy $ qabul qilingani payload.cash_given_amount'da o'z
            # holicha saqlanadi, faqat MoySklad hujjatining o'zi so'mda.
            given_in_som = (
                payload.cash_given_amount * payload.exchange_rate
                if payload.currency_meta and payload.exchange_rate
                else payload.cash_given_amount
            )
            given_minor = _to_minor_units(given_in_som)
            payment_body = {
                "organization": {"meta": _org_meta()},
                "agent": {"meta": agent_meta},
                "applicable": True,
                "sum": given_minor,
                "organizationAccount": {"meta": payload.account_meta},
                "operations": [{"meta": order_meta, "linkedSum": given_minor}],
            }
            if payload.payment_moment:
                payment_body["moment"] = payload.payment_moment
            endpoint = "/entity/cashin" if payload.document_type == "cashin" else "/entity/paymentin"
            payment = await ms_request(
                "POST", endpoint, token=token, json=payment_body, timeout=_DOC_CREATE_TIMEOUT_SECONDS
            )
            payment_ids = {"ms_payment_id": payment["id"], "ms_payment_name": payment.get("name") or ""}

            if payload.cash_change_som:
                change_account_meta = await _get_account_meta_by_name(token, SHOP_SOM_ACCOUNT_NAME)
                article_meta = await _get_change_expense_article_meta(token)
                cashout_body = {
                    "organization": {"meta": _org_meta()},
                    "agent": {"meta": agent_meta},
                    "applicable": True,
                    "sum": _to_minor_units(payload.cash_change_som),
                    "organizationAccount": {"meta": change_account_meta},
                }
                if article_meta:
                    cashout_body["expenseItem"] = {"meta": article_meta}
                await ms_request(
                    "POST", "/entity/cashout", token=token, json=cashout_body, timeout=_DOC_CREATE_TIMEOUT_SECONDS
                )
        else:
            payment_body = {
                "organization": {"meta": _org_meta()},
                "agent": {"meta": agent_meta},
                "applicable": True,
                "sum": row_sum_minor,
                "organizationAccount": {"meta": payload.account_meta},
                "operations": [{"meta": order_meta, "linkedSum": row_sum_minor}],
            }
            if payload.payment_moment:
                payment_body["moment"] = payload.payment_moment
            endpoint = "/entity/cashin" if payload.document_type == "cashin" else "/entity/paymentin"
            payment = await ms_request(
                "POST", endpoint, token=token, json=payment_body, timeout=_DOC_CREATE_TIMEOUT_SECONDS
            )
            payment_ids = {"ms_payment_id": payment["id"], "ms_payment_name": payment.get("name") or ""}
    except HTTPException as exc:
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_FAILED,
            last_error=str(exc.detail),
            last_attempt_at=sheets_client.now_iso(),
        )
        return "failed"
    except Exception as exc:
        logger.exception("Qator %s: kutilmagan xatolik", order_id)
        await sheets_client.update_row(
            order_id,
            status=sheets_client.STATUS_NEEDS_MANUAL_CHECK,
            last_error=f"Kutilmagan xatolik: {exc}",
            last_attempt_at=sheets_client.now_iso(),
        )
        return "needs_manual_check"

    await sheets_client.update_row(
        order_id,
        status=sheets_client.STATUS_SYNCED,
        synced_at=sheets_client.now_iso(),
        ms_order_id=daily_order["ms_order_id"],
        ms_order_name=daily_order["ms_order_name"],
        ms_demand_id=daily_order["ms_demand_id"],
        ms_demand_name=daily_order["ms_demand_name"],
        **payment_ids,
    )
    stock_cache.release_order(order_id)
    return "synced"


async def _sync_one_business_day(business_day: str, rows: list, token: str) -> dict:
    daily = await sheets_client.get_daily_order(business_day)
    if daily is None:
        await sheets_client.create_daily_order(business_day)
        daily = await sheets_client.get_daily_order(business_day)

    if daily["status"] in (sheets_client.DAILY_STATUS_PENDING,) or (
        daily["status"] == sheets_client.DAILY_STATUS_SYNCING and not daily.get("chain_started_at")
    ):
        claimed = await sheets_client.compare_and_set_daily_status(
            business_day,
            {sheets_client.DAILY_STATUS_PENDING, sheets_client.DAILY_STATUS_SYNCING},
            sheets_client.DAILY_STATUS_SYNCING,
        )
        if claimed:
            await sheets_client.update_daily_order(business_day, chain_started_at=sheets_client.now_iso())
            try:
                await _create_daily_order_and_demand(business_day, rows, token)
            except (RollbackFailedError, DailyOrderAmbiguousError) as exc:
                await sheets_client.update_daily_order(
                    business_day, status=sheets_client.DAILY_STATUS_NEEDS_MANUAL_CHECK, last_error=str(exc)
                )
                return {"business_day": business_day, "outcome": "needs_manual_check", "rows": 0}
            except HTTPException as exc:
                # MoySklad'ga hali tegilmagan (yoki rollback muvaffaqiyatli
                # bo'lgan) oddiy xato — xavfsiz qayta urinish uchun "pending"ga
                # qaytariladi, "chain_started_at" ataylab tozalanadi.
                await sheets_client.update_daily_order(
                    business_day,
                    status=sheets_client.DAILY_STATUS_PENDING,
                    chain_started_at="",
                    last_error=str(exc.detail),
                )
                return {"business_day": business_day, "outcome": "failed", "rows": 0}
        # Qayta yuklaymiz — claimed=True bo'lsa yangi holatni, False bo'lsa
        # boshqa parallel jarayon o'zgartirgan haqiqiy holatni olish uchun.
        daily = await sheets_client.get_daily_order(business_day)

    if daily["status"] != sheets_client.DAILY_STATUS_SYNCED:
        # Hali tayyor emas: boshqa jarayon band qilgan ("syncing") yoki
        # noaniq holat ("needs_manual_check") — bu safar hech narsa qilmaymiz.
        return {"business_day": business_day, "outcome": daily["status"], "rows": 0}

    outcomes = {"synced": 0, "failed": 0, "needs_manual_check": 0}
    for row in rows:
        if row["status"] not in (sheets_client.STATUS_PENDING, sheets_client.STATUS_FAILED):
            continue
        outcome = await _sync_row_payment(row, daily, token)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    return {"business_day": business_day, "outcome": "processed", **outcomes}


async def run_shop_sync() -> dict:
    global _running
    if not SHOP_ORGANIZATION_ID:
        return {"error": "SHOP_ORGANIZATION_ID sozlanmagan", "days": []}
    if _running:
        return {"skipped": True, "reason": "Do'kon sinxronizatsiyasi allaqachon ishlamoqda", "days": []}

    _running = True
    try:
        try:
            token = await get_shared_admin_token()
        except HTTPException as exc:
            return {"error": f"Token olinmadi: {exc.detail}", "days": []}

        grouped = await sheets_client.get_syncable_rows_by_business_day()
        results = []
        for business_day, rows in grouped.items():
            result = await _sync_one_business_day(business_day, rows, token)
            results.append(result)
        return {"days": results}
    finally:
        _running = False
