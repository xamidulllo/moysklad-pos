"""Butun tovar katalogi (entity/assortment) va mijozlar ro'yxatini
(entity/counterparty) xotirada saqlaydi — qidiruv har safar MoySklad'ga
so'rov yubormasdan, to'g'ridan-to'g'ri xotiradan (deyarli oniy) javob berishi
uchun. Ma'lumot 1 soatga eskirgach, KEYINGI so'rov uni emas, FONDAGI alohida
vazifa yangilaydi (pastdagi ensure_fresh()'ga qarang) — chunki rasmlar bilan
to'liq katalogni qayta yuklash o'nlab soniya olishi mumkin (quyidagi eslatma),
buni kassirning bitta so'rovi ichida kutdirib bo'lmaydi.

MUHIM (xavfsizlik): kesh MoySklad hisobi (accountId) bo'yicha AJRATILGAN —
turli MoySklad login'lar turli hisoblarga tegishli bo'lishi mumkin (ilova
istalgan login bilan kirishga ruxsat beradi), shuning uchun bitta umumiy
(global) kesh ishlatilsa, boshqa hisobning kassiri avvalgi hisobning tovar/
mijoz ro'yxatini ko'rib qolishi mumkin edi. Har bir accountId o'zining
alohida ro'yxatiga ega.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import HTTPException

from . import sheets_client
from .moysklad_client import ms_request

logger = logging.getLogger("moysklad_pos.catalog_cache")

# MUHIM (real productionda to'g'ridan-to'g'ri o'lchab tekshirilgan, 2026-08-29):
# hozir MoySklad'ning bu hisobida "entity/assortment"ga limit=50 — 0.7 soniyada
# javob beradi, LEKIN limit=100 va undan katta HAR QANDAY qiymat 45+ soniya
# ichida UMUMAN javob bermaydi (bir nechta marta, izchil tasdiqlangan — bu
# tasodif emas, aynan shu hisobda hozir joriy bo'lgan qattiq chegara). Shu
# sabab sahifa hajmi qasddan kichik qilib belgilangan — ko'proq so'rov, lekin
# HAR BIRI tez va ishonchli, kattaroq-lekin-osilib-qoladigan bitta so'rovdan
# ko'ra ancha yaxshi.
REFRESH_INTERVAL_SECONDS = 3600
_PAGE_SIZE = 50
# Umumiy httpx klient standart bo'yicha 25s timeout ishlatadi (interaktiv
# so'rovlar uchun mos — kassir tezda javob kutadi). Lekin butun katalogni
# (o'nlab sahifa) yuklash paytida MoySklad ba'zan bitta sahifaga shuncha
# vaqtdan ko'proq javob berishi mumkin (real productionda ReadTimeout bilan
# tasdiqlangan) — bu yangilash fonda ketadi, kassirni kutdirmaydi, shuning
# uchun shu bitta chaqiruv turi uchun ancha kattaroq timeout ishlatiladi.
_BULK_TIMEOUT_SECONDS = 60.0
_BULK_MAX_ATTEMPTS = 3
# MUHIM (real API'da to'g'ridan-to'g'ri o'lchab tekshirilgan): "expand=images"
# "limit" taxminan 100 dan oshganda JIM RAVISHDA ishlamay qoladi — MoySklad
# rasm ma'lumotini butunlay tashlab yuboradi (xato bermaydi, shunchaki "images"
# maydoni bo'sh qaytadi). limit=100'da 40/100 tovarda rasm bor edi, limit=150'da
# 0/150. 2026-08-29'da esa limit=100 (rasmsiz ham) butunlay javobsiz osilib
# qolishi alohida tasdiqlandi (yuqoridagi _PAGE_SIZE'ga qarang) — shu sabab bu
# ham xuddi shu xavfsiz kichik qiymatga tushirilgan.
_ASSORTMENT_PAGE_SIZE = _PAGE_SIZE

# VAQTINCHALIK (2026-08-26): MoySklad hozir bitta sahifaga ba'zan 60+ soniya
# javob berayotgani sabab, rasmlar uchun kerak bo'lgan 100'lik kichik sahifa
# hajmi (o'ndan ortiq alohida so'rov) butun katalogni yuklashni daqiqalab
# cho'zib, qidiruvni ishlatib bo'lmas holga keltirdi. MoySklad'ning javob
# tezligi tiklangach, INCLUDE_PRODUCT_IMAGES=true qilib rasmlarni qaytarish
# mumkin — hozircha ishlash (funksiyaning o'zi) rasmlardan ustun.
INCLUDE_IMAGES = os.getenv("INCLUDE_PRODUCT_IMAGES", "false").strip().lower() == "true"

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


async def _fetch_page_with_retry(path: str, token: str, params: dict) -> dict:
    """Bitta sahifani (MoySklad'ning vaqtinchalik sekinligiga chidamli holda)
    yuklaydi — bir sahifadagi ReadTimeout butun ko'p o'nlab so'rovli katalog
    yangilashini boshidan boshlashga majburlamasligi uchun bir necha marta
    qayta uriniladi, har safar avvalgisidan ko'proq kutib."""
    last_exc: Exception | None = None
    for attempt in range(_BULK_MAX_ATTEMPTS):
        try:
            return await ms_request(
                "GET", path, token=token, params=params, timeout=_BULK_TIMEOUT_SECONDS
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning(
                "Kataloq sahifasini yuklashda vaqtinchalik xatolik (%s, urinish %d/%d): %s",
                path, attempt + 1, _BULK_MAX_ATTEMPTS, exc,
            )
            if attempt < _BULK_MAX_ATTEMPTS - 1:
                await asyncio.sleep(2.0 * (attempt + 1))
    raise last_exc


async def _fetch_all(path: str, token: str, page_size: int = _PAGE_SIZE, **params) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        data = await _fetch_page_with_retry(
            path, token, params={"limit": page_size, "offset": offset, **params}
        )
        rows = data.get("rows", [])
        items.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return items


_SHEETS_SNAPSHOT_SAVE_TIMEOUT_SECONDS = 60.0


async def _save_snapshot_best_effort(assortment: list[dict], counterparties: list[dict]) -> None:
    """MoySklad'dan yangi olingan katalogni Sheets'ga ham yozadi — keyingi
    qayta ishga tushirishda (yoki MoySklad hozircha ishlamasa) shu "so'nggi
    ma'lum" nusxadan darhol foydalanish uchun. Xatolik bo'lsa ham asosiy
    (xotiradagi) keshga ta'sir qilmaydi — faqat logga yoziladi.

    Bu fon vazifasi ichida ishlagani uchun osilib qolish kassirni to'g'ridan-
    to'g'ri to'xtatmaydi, lekin cheklanmagan vaqt kutish asta-sekin
    `asyncio.to_thread`'ning umumiy thread hovuzini band qilib, boshqa barcha
    (aloqasiz) fon ishlarini ham asta sekinlashtirib qo'yishi mumkin edi —
    shu sabab bu yerda ham qat'iy timeout bor."""
    try:
        await asyncio.wait_for(
            sheets_client.save_catalog_snapshot(assortment), timeout=_SHEETS_SNAPSHOT_SAVE_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(
            sheets_client.save_customers_snapshot(counterparties), timeout=_SHEETS_SNAPSHOT_SAVE_TIMEOUT_SECONDS
        )
        logger.info("Katalog suratlanmasi Sheets'ga saqlandi (%d tovar, %d mijoz)", len(assortment), len(counterparties))
    except Exception:
        logger.exception("Katalog suratlanmasini Sheets'ga saqlab bo'lmadi (yoki javob bermadi) — xotiradagi kesh baribir yangilangan")


async def _refresh(account_id: str, token: str) -> None:
    if INCLUDE_IMAGES:
        assortment_fetch = _fetch_all("/entity/assortment", token, page_size=_ASSORTMENT_PAGE_SIZE, expand="images")
    else:
        assortment_fetch = _fetch_all("/entity/assortment", token, page_size=_PAGE_SIZE)
    assortment, counterparties = await asyncio.gather(
        assortment_fetch,
        _fetch_all("/entity/counterparty", token),
    )
    _assortment[account_id] = assortment
    _counterparties[account_id] = counterparties
    _last_refresh[account_id] = time.time()

    # Sheets'ga yozish sekin bo'lishi mumkin (Google API) — chaqiruvchini
    # (kassirni yoki fon vazifasini) bu bilan kutdirmaymiz, alohida vazifada
    # ketadi.
    save_task = asyncio.create_task(_save_snapshot_best_effort(assortment, counterparties))
    _background_tasks.add(save_task)
    save_task.add_done_callback(_background_tasks.discard)


_refreshing: dict[str, bool] = {}
# asyncio.create_task() qaytargan Task'ga hech kim havola saqlamasa, u tugashidan
# OLDIN chiqindi yig'uvchi (garbage collector) tomonidan olib tashlanishi mumkin
# (Python'ning tasdiqlangan xatti-harakati) — shu sabab tugagunicha shu to'plamda
# saqlanadi.
_background_tasks: set = set()


_SHEETS_SNAPSHOT_READ_TIMEOUT_SECONDS = 8.0


async def _try_load_from_sheets_snapshot(account_id: str) -> bool:
    """MoySklad'ga umuman tegmasdan, oldin saqlangan Sheets suratlanmasidan
    kataloqni darhol tiklashga urinadi. MoySklad hozir ishlamasa/sekin
    bo'lsa ham, qidiruv "eski, lekin bor" ma'lumot bilan ishlashda davom
    etishi uchun (real productionda MoySklad'ning bir necha soatlab
    javob bermay qolgani tasdiqlangan).

    MUHIM (real productionda topilgan xato, 2026-08-29): Google Sheets/OAuth
    client kutubxonasi (gspread) o'zining HTTP so'rovlariga HECH QANDAY
    timeout qo'ymaydi — agar Google tomoni birinchi (hali worksheet
    yaratilmagan) so'rovda kutilganidan sekin javob bersa, shu chaqiruv
    ABADIY osilib qolishi mumkin edi, bu esa "suratlanma — faqat qo'shimcha
    tezlashtirish" degan asosiy g'oyani teskarisiga aylantirar edi (butun
    kassa ishini to'xtatib qo'yardi). Shu sabab bu yerga qat'iy tashqi
    timeout qo'yilgan — muddat o'tsa, oddiy MoySklad'dan yuklash yo'liga
    tushiladi (aynan Sheets ishlamagandagi kabi)."""
    try:
        assortment, counterparties = await asyncio.wait_for(
            asyncio.gather(
                sheets_client.load_catalog_snapshot(),
                sheets_client.load_customers_snapshot(),
            ),
            timeout=_SHEETS_SNAPSHOT_READ_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Sheets suratlanmasini o'qib bo'lmadi (yoki javob bermadi) — oddiy MoySklad yo'liga tushilmoqda")
        return False
    if not assortment and not counterparties:
        return False
    _assortment[account_id] = assortment
    _counterparties[account_id] = counterparties
    logger.info(
        "Katalog Sheets suratlanmasidan tiklandi (%d tovar, %d mijoz) — MoySklad'dan yangilanish fonda davom etadi",
        len(assortment), len(counterparties),
    )
    return True


async def ensure_fresh(account_id: str, token: str) -> None:
    """Kesh HALI UMUMAN to'ldirilmagan bo'lsa — avval Sheets'dagi so'nggi
    ma'lum suratlanmadan (agar bor bo'lsa) DARHOL tiklanadi, MoySklad'dan
    haqiqiy yangilanish esa fonda, kassirni kutdirmasdan ketadi. Faqat
    Sheets'da ham hech narsa bo'lmasa (masalan ilovaning eng birinchi
    ishga tushishi), chaqiruvchi so'rovning o'zida MoySklad'ni kutadi
    (ko'rsatadigan boshqa ma'lumot yo'q, ilojsiz).

    Lekin kesh ALLAQACHON bор, faqat 1 soatdan eski bo'lsa — chaqiruvchini
    UMUMAN kutdirmaydi: eskirgan (lekin mavjud) ma'lumot darhol qaytariladi,
    yangilash esa fonda, alohida vazifada boshlanadi."""
    if account_id not in _last_refresh:
        async with _get_lock(account_id):
            if account_id not in _last_refresh:
                if await _try_load_from_sheets_snapshot(account_id):
                    # Kesh "eskirgan" deb belgilanadi — pastdagi oddiy fon-
                    # yangilash yo'li (REFRESH_INTERVAL_SECONDS tekshiruvi)
                    # MoySklad'dan haqiqiy yangilanishni o'zi boshlab yuboradi,
                    # kassirni bu safar ham kutdirmasdan.
                    _last_refresh[account_id] = 0.0
                else:
                    try:
                        await _refresh(account_id, token)
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        logger.exception("Birinchi kataloq yuklashi muvaffaqiyatsiz")
                        raise HTTPException(
                            status_code=503,
                            detail="Katalog hali yuklanmoqda, birozdan keyin qayta urinib ko'ring",
                        ) from exc
                    return

    if time.time() - _last_refresh[account_id] < REFRESH_INTERVAL_SECONDS:
        return

    if _refreshing.get(account_id):
        return  # fonda yangilash allaqachon ketyapti — yana bittasini boshlamaymiz

    async def _background_refresh() -> None:
        _refreshing[account_id] = True
        try:
            async with _get_lock(account_id):
                if time.time() - _last_refresh.get(account_id, 0.0) >= REFRESH_INTERVAL_SECONDS:
                    await _refresh(account_id, token)
        except Exception:
            logger.exception("Fon kataloq yangilashda xatolik — eski kesh davom etadi")
        finally:
            _refreshing[account_id] = False

    task = asyncio.create_task(_background_refresh())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


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
