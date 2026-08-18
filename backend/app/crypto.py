"""Kassirning MoySklad parolini shifrlab saqlash — FAQAT navbatga qo'yilgan
buyurtmalarni keyinroq AYNAN o'sha kassirning o'zi nomidan sinxronlash uchun
kerak (sync_job.py).

MUHIM: bu parolni ODDIY holda saqlashdan farqli — Fernet (AES128-CBC + HMAC)
bilan shifrlanadi, kalit esa faqat serverda, `CREDENTIAL_ENCRYPTION_KEY` orqali
saqlanadi. Shifrlangan qiymat Google Sheets qatorida turadi (chunki sync soatlab
keyin ishlaydi, kassirning sessiyasi tugab ketgan bo'lishi mumkin) — kalit
oshkor bo'lmasa, Sheets'ning o'zi ochilib qolsa ham parol o'qib bo'lmaydi.
"""
from cryptography.fernet import Fernet, InvalidToken

from .config import CREDENTIAL_ENCRYPTION_KEY

_fernet = Fernet(CREDENTIAL_ENCRYPTION_KEY.encode()) if CREDENTIAL_ENCRYPTION_KEY else None


class CredentialEncryptionNotConfigured(Exception):
    """CREDENTIAL_ENCRYPTION_KEY sozlanmagan — "queue" rejimi buni talab qiladi."""


def encrypt_password(raw_password: str) -> str:
    if not _fernet:
        raise CredentialEncryptionNotConfigured("CREDENTIAL_ENCRYPTION_KEY sozlanmagan")
    return _fernet.encrypt(raw_password.encode("utf-8")).decode("utf-8")


def decrypt_password(encrypted_password: str) -> str:
    if not _fernet:
        raise CredentialEncryptionNotConfigured("CREDENTIAL_ENCRYPTION_KEY sozlanmagan")
    try:
        return _fernet.decrypt(encrypted_password.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Parolni ochib bo'lmadi — shifrlash kaliti o'zgargan yoki qiymat buzilgan") from exc
