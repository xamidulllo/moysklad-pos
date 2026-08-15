"""MoySklad REST API (remap/1.2) uchun ingichka HTTP klient.

Frontend hech qachon MoySklad'ga to'g'ridan-to'g'ri murojaat qilmaydi. Endi
global token ham yo'q — har bir chaqiruv joriy kassirning sessiyasiga tegishli
tokenni aniq argument sifatida oladi (token faqat serverda, sessiya ichida
saqlanadi, brauzerga hech qachon jo'natilmaydi).
"""
from typing import Optional

import httpx
from fastapi import HTTPException

from .config import MOYSKLAD_BASE_URL


async def ms_request(method: str, path: str, token: str, **kwargs) -> Optional[dict]:
    """MoySklad'ga so'rov yuboradi; xatolik bo'lsa uni FastAPI HTTPException'ga aylantiradi
    (asl MoySklad xato matni bilan birga), shunda frontend aniq sababni ko'rsata oladi."""
    async with httpx.AsyncClient(
        base_url=MOYSKLAD_BASE_URL,
        timeout=25.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        response = await client.request(method, path, **kwargs)

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
    async with httpx.AsyncClient(base_url=MOYSKLAD_BASE_URL, timeout=20.0) as client:
        response = await client.post("/security/token", auth=(login, password))

    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    return response.json()["access_token"]
