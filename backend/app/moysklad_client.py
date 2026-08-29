"""MoySklad REST API (remap/1.2) uchun ingichka HTTP klient.

Frontend hech qachon MoySklad'ga to'g'ridan-to'g'ri murojaat qilmaydi. Endi
global token ham yo'q — har bir chaqiruv joriy kassirning sessiyasiga tegishli
tokenni aniq argument sifatida oladi (token faqat serverda, sessiya ichida
saqlanadi, brauzerga hech qachon jo'natilmaydi).

MUHIM (real productionda o'lchab tekshirilgan): har bir chaqiruv uchun YANGI
`httpx.AsyncClient` ochish (avvalgi versiya shunday qilar edi) har safar yangi
TCP+TLS ulanish o'rnatishga majbur qiladi — bu qidiruv ekranidagi 3 ta parallel
so'rovni ~0.3s o'rniga ~1.7s ga cho'zib yuborar edi (5-6 baravar sekinroq).
Shu sabab bitta umumiy, butun process davomida qayta ishlatiladigan client
ishlatiladi — httpx.AsyncClient aynan shunday, ko'p vazifa/so'rov orasida
xavfsiz ulashiladigan qilib mo'ljallangan (o'ziga xos connection pool bilan)."""
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import HTTPException

from .config import MOYSKLAD_BASE_URL

logger = logging.getLogger("moysklad_pos.moysklad_client")

_client: "httpx.AsyncClient | None" = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=MOYSKLAD_BASE_URL, timeout=25.0)
    return _client


async def close_client() -> None:
    """Ilova to'xtaganda chaqiriladi (main.py'ning lifespan()'iga qarang) —
    ochiq ulanishlarni toza yopish uchun."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ms_request(method: str, path: str, token: str, **kwargs) -> Optional[dict]:
    """MoySklad'ga so'rov yuboradi; xatolik bo'lsa uni FastAPI HTTPException'ga aylantiradi
    (asl MoySklad xato matni bilan birga), shunda frontend aniq sababni ko'rsata oladi."""
    headers = kwargs.pop("headers", None) or {}
    headers["Authorization"] = f"Bearer {token}"
    response = await _get_client().request(method, path, headers=headers, **kwargs)

    if response.status_code in (401, 403):
        # MUHIM (2026-08-29): ilova endi FAQAT bitta umumiy MoySklad hisobidan
        # foydalanadi (individual kassir login'lari olib tashlandi) — shu
        # sabab 401/403 deyarli har doim shu tokenning boshqa joyda (masalan
        # fon vazifasi yoki bir vaqtdagi boshqa so'rov tomonidan) yangilanib/
        # bekor qilingani ma'nosini anglatadi, noto'g'ri kalit emas. Shu
        # sabab bu yerda, ENG PASTKI darajada (har bir marshrutga qo'lda
        # takrorlash o'rniga), tokenni majburiy yangilab BIR MARTA avtomatik
        # qayta uriniladi. Aylanma importdan qochish uchun import shu yerda
        # (sync_job.py o'zi ham shu modulni import qiladi).
        from .sync_job import get_shared_admin_token
        try:
            fresh_token = await get_shared_admin_token(force_refresh=True)
        except Exception:
            fresh_token = None
        if fresh_token and fresh_token != token:
            logger.warning("MoySklad token bekor qilingan (%s %s, %d) — yangilab qayta urinilmoqda", method, path, response.status_code)
            headers["Authorization"] = f"Bearer {fresh_token}"
            response = await _get_client().request(method, path, headers=headers, **kwargs)

    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


async def ms_request_resilient(
    method: str, path: str, token: str, *, attempts: int = 3, retry_timeout: float = 45.0, **kwargs
) -> Optional[dict]:
    """`ms_request`ning xuddi o'zi, lekin MoySklad'ning vaqtinchalik
    sekinligiga (tarmoq/vaqt tugashi) chidamli — bir necha marta, ancha
    kattaroq timeout bilan qayta uriniladi. Faqat CHIN (409/400/... kabi)
    MoySklad rad etishlari darhol yuqoriga uzatiladi, faqat ReadTimeout/
    tarmoq xatolari qayta uriniladi.

    MUHIM (real productionda tasdiqlangan, 2026-08-25/26): MoySklad ba'zan
    standart 25s'dan ancha ko'proq vaqt olib javob beradi — bu ayniqsa
    kesh hali to'ldirilmagan BIRINCHI so'rovda (kunning birinchi kassiri/
    qidiruvi) sezilarli, chunki keyingi so'rovlar keshdan (1 soatga)
    javob oladi va bu funksiyaga umuman murojaat qilmaydi."""
    last_exc: "Exception | None" = None
    for attempt in range(attempts):
        try:
            return await ms_request(method, path, token=token, timeout=retry_timeout, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning(
                "MoySklad so'rovida vaqtinchalik xatolik (%s %s, urinish %d/%d): %s",
                method, path, attempt + 1, attempts, exc,
            )
            if attempt < attempts - 1:
                await asyncio.sleep(2.0 * (attempt + 1))
    raise last_exc


async def exchange_credentials_for_token(login: str, password: str) -> str:
    """Kassirning MoySklad login/parolini access_token'ga almashtiradi.

    Haqiqiy MoySklad hisobida tekshirilgan: POST /security/token + Basic auth
    (login, password) -> 201 {"access_token": "..."}. Noto'g'ri parolda MoySklad
    401 qaytaradi — shu status va xabar o'zgarishsiz frontend'ga uzatiladi.
    """
    response = await _get_client().post("/security/token", auth=(login, password), timeout=20.0)

    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    return response.json()["access_token"]
