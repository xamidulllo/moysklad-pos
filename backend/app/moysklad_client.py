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
from typing import Optional

import httpx
from fastapi import HTTPException

from .config import MOYSKLAD_BASE_URL

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

    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    if response.status_code == 204 or not response.content:
        return None
    return response.json()


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
