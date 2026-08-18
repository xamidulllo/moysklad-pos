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

from fastapi import Cookie, Header, HTTPException

from .config import SESSION_TTL_HOURS, SYNC_TRIGGER_SECRET

_sessions: dict[str, dict] = {}


def create_session(
    token: str,
    employee_name: str,
    login: str,
    password_enc: "str | None" = None,
) -> str:
    """`login`/`password_enc` — "queue" rejimida navbatga qo'yilgan buyurtmani
    keyinroq AYNAN shu kassir nomidan sinxronlash uchun kerak (sync soatlab
    keyin ishlaganda, bu sessiya allaqachon tugagan bo'lishi mumkin — shu
    sabab checkout paytida bu qiymatlar Sheets qatoriga ko'chirib qo'yiladi,
    main.py'ga qarang). `password_enc` — crypto.py orqali OLDINDAN shifrlangan
    qiymat, hech qachon ochiq (plaintext) holda saqlanmaydi."""
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
        "token": token,
        "employee_name": employee_name,
        "login": login,
        "password_enc": password_enc,
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


async def require_sync_secret(x_sync_secret: Optional[str] = Header(default=None)) -> None:
    """Faqat Google Apps Script trigger'i chaqirishi kerak bo'lgan /api/sync/run
    marshrutini himoya qiladi. Maxfiy qiymat query parametrda emas, header'da
    kutiladi — aks holda Render'ning kirish loglariga tushib qolardi.
    secrets.compare_digest — vaqt hujumidan (timing attack) himoya uchun."""
    if not SYNC_TRIGGER_SECRET or not x_sync_secret or not secrets.compare_digest(x_sync_secret, SYNC_TRIGGER_SECRET):
        raise HTTPException(status_code=401, detail="Noto'g'ri yoki sozlanmagan sync maxfiy kaliti")
