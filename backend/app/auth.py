"""Kassir sessiyalarini boshqarish.

Kassir /api/login orqali o'zining MoySklad login/parolini yuboradi, backend buni
tokenga almashtiradi (moysklad_client.exchange_credentials_for_token) va natijani
shu yerda, xotirada, tasodifiy sessiya ID'siga bog'lab saqlaydi. Brauzerga faqat
shu tasodifiy ID httpOnly cookie sifatida jo'natiladi — haqiqiy MoySklad tokeni
hech qachon brauzerga chiqmaydi.

ESLATMA: sessiyalar jarayon xotirasida saqlanadi — server qayta ishga tushirilsa,
barcha kassirlar qaytadan kirishi kerak bo'ladi. Bir nechta backend nusxasi
(masalan load balancer ortida) ishlatiladigan production muhitda buni Redis kabi
umumiy xotiraga ko'chirish kerak.
"""
import secrets
import time
from typing import Optional

from fastapi import Cookie, HTTPException

from .config import SESSION_TTL_HOURS

_sessions: dict[str, dict] = {}


def create_session(token: str, employee_name: str) -> str:
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "token": token,
        "employee_name": employee_name,
        "created": time.time(),
    }
    return session_id


def _get_session(session_id: Optional[str]) -> Optional[dict]:
    if not session_id:
        return None
    session = _sessions.get(session_id)
    if not session:
        return None
    if time.time() - session["created"] > SESSION_TTL_HOURS * 3600:
        _sessions.pop(session_id, None)
        return None
    return session


def delete_session(session_id: Optional[str]) -> None:
    if session_id:
        _sessions.pop(session_id, None)


async def get_current_session(pos_session: Optional[str] = Cookie(default=None)) -> dict:
    session = _get_session(pos_session)
    if not session:
        raise HTTPException(status_code=401, detail="Tizimga kirish talab qilinadi")
    return session


async def get_current_token(pos_session: Optional[str] = Cookie(default=None)) -> str:
    session = _get_session(pos_session)
    if not session:
        raise HTTPException(status_code=401, detail="Tizimga kirish talab qilinadi")
    return session["token"]
