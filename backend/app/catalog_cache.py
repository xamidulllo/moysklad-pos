"""Butun tovar katalogi (entity/assortment) va mijozlar ro'yxatini
(entity/counterparty) xotirada saqlaydi — qidiruv har safar MoySklad'ga
so'rov yubormasdan, to'g'ridan-to'g'ri xotiradan (deyarli oniy) javob berishi
uchun. Ma'lumot 1 soatga eskirgach, KEYINGI so'rov uni fonda emas, o'sha
so'rovning o'zida yangilaydi — alohida background vazifa/hardcoded login
kerak emas, chunki bu multi-tenant ilova (istalgan MoySklad login ishlaydi)
va faqat HAQIQATDA ishlatilayotgan hisoblar uchun keshlash kifoya.

MUHIM (xavfsizlik): kesh MoySklad hisobi (accountId) bo'yicha AJRATILGAN —
turli MoySklad login'lar turli hisoblarga tegishli bo'lishi mumkin (ilova
istalgan login bilan kirishga ruxsat beradi), shuning uchun bitta umumiy
(global) kesh ishlatilsa, boshqa hisobning kassiri avvalgi hisobning tovar/
mijoz ro'yxatini ko'rib qolishi mumkin edi. Har bir accountId o'zining
alohida ro'yxatiga ega.
"""
import asyncio
import time
from typing import Optional

from .moysklad_client import ms_request

REFRESH_INTERVAL_SECONDS = 3600
_PAGE_SIZE = 1000

_assortment: dict[str, list[dict]] = {}
_counterparties: dict[str, list[dict]] = {}
_last_refresh: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(account_id: str) -> asyncio.Lock:
    lock = _locks.get(account_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[account_id] = lock
    return lock


async def _fetch_all(path: str, token: str, **params) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        data = await ms_request(
            "GET", path, token=token, params={"limit": _PAGE_SIZE, "offset": offset, **params}
        )
        rows = data.get("rows", [])
        items.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return items


async def _refresh(account_id: str, token: str) -> None:
    assortment, counterparties = await asyncio.gather(
        _fetch_all("/entity/assortment", token, expand="images"),
        _fetch_all("/entity/counterparty", token),
    )
    _assortment[account_id] = assortment
    _counterparties[account_id] = counterparties
    _last_refresh[account_id] = time.time()


async def ensure_fresh(account_id: str, token: str) -> None:
    """Kesh hali umuman to'ldirilmagan yoki 1 soatdan eski bo'lsa, shu yerning
    o'zida (chaqiruvchi so'rov ichida) yangilaydi. Lock — bir vaqtda bir nechta
    so'rov keshni bir vaqtda ikki marta yangilab, MoySklad'ga ortiqcha
    so'rov yubormasligi uchun."""
    last = _last_refresh.get(account_id, 0.0)
    if time.time() - last < REFRESH_INTERVAL_SECONDS:
        return
    async with _get_lock(account_id):
        last = _last_refresh.get(account_id, 0.0)
        if time.time() - last < REFRESH_INTERVAL_SECONDS:
            return  # boshqa so'rov shu orada allaqachon yangilagan
        await _refresh(account_id, token)


def _matches(row: dict, query: str) -> bool:
    q = query.lower()
    return (
        q in (row.get("name") or "").lower()
        or q in (row.get("code") or "").lower()
        or q in (row.get("article") or "").lower()
    )


def search_assortment(account_id: str, query: str) -> list[dict]:
    rows = _assortment.get(account_id, [])
    if not query:
        return rows
    return [r for r in rows if _matches(r, query)]


def find_by_barcode(account_id: str, code: str) -> Optional[dict]:
    for row in _assortment.get(account_id, []):
        for b in row.get("barcodes") or []:
            if code in b.values():
                return row
    return None


def search_counterparties(account_id: str, query: str) -> list[dict]:
    rows = _counterparties.get(account_id, [])
    if not query:
        return rows
    q = query.lower()
    return [r for r in rows if q in (r.get("name") or "").lower()]


def add_counterparty(account_id: str, row: dict) -> None:
    """Yangi yaratilgan mijozni darhol keshga ham qo'shamiz — aks holda soat
    davomida shu mijozni (hatto o'zi yaratgan kassir ham) qidirib topa
    olmas edi."""
    _counterparties.setdefault(account_id, []).append(row)
