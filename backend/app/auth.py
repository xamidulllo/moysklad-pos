"""Kassir sessiyalarini boshqarish.

MUHIM (2026-08-29'da qayta qurilgan): ilova endi shaxsiy MoySklad login/parol
so'ramaydi — bu faqat avtoservis o'zining yopiq jamoasi ishlatadigan, bitta
MoySklad hisobiga ulangan tizim, shuning uchun har bir kassirning o'z alohida
MoySklad tokeniga ehtiyoj yo'q edi (aslida hammasi baribir BITTA umumiy
hisobdan kirar edi — shu sabab bu shunchaki qo'shimcha sekinlik va token
to'qnashuvlari manbai edi, xavfsizlik emas). Kirish endi faqat kassir ismini
(hisobot uchun) yozib, mahalliy sessiya yaratadi — MoySklad'ga umuman
so'rov yubormaydi. Haqiqiy MoySklad tokeni endi sessiyada saqlanmaydi;
kerak bo'lganda sync_job.get_shared_admin_token() orqali umumiy, keshlangan
hisobdan olinadi (barcha marshrutlar uchun bitta manba — token to'qnashuvi
muammosining ildizidan yechimi).

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
from .sync_job import get_shared_admin_token

_sessions: dict[str, dict] = {}


def create_session(employee_name: str) -> str:
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = {
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
    return await get_shared_admin_token()


async def require_sync_secret(x_sync_secret: Optional[str] = Header(default=None)) -> None:
    """Faqat Google Apps Script trigger'i chaqirishi kerak bo'lgan /api/sync/run
    marshrutini himoya qiladi. Maxfiy qiymat query parametrda emas, header'da
    kutiladi — aks holda Render'ning kirish loglariga tushib qolardi.
    secrets.compare_digest — vaqt hujumidan (timing attack) himoya uchun."""
    if not SYNC_TRIGGER_SECRET or not x_sync_secret or not secrets.compare_digest(x_sync_secret, SYNC_TRIGGER_SECRET):
        raise HTTPException(status_code=401, detail="Noto'g'ri yoki sozlanmagan sync maxfiy kaliti")
