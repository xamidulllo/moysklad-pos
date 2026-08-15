"""Ilova sozlamalari.

Endi bitta global MoySklad tokeni talab qilinmaydi — har bir kassir o'z shaxsiy
MoySklad login/paroli bilan kiradi (/api/login), backend buni MoySklad'ning o'z
POST /security/token xizmati orqali tokenga almashtiradi va vaqtinchalik
server-side sessiya sifatida saqlaydi (auth.py'ga qarang).
"""
import os
from dotenv import load_dotenv

load_dotenv()

MOYSKLAD_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

SESSION_COOKIE_NAME = "pos_session"
SESSION_TTL_HOURS = float(os.getenv("SESSION_TTL_HOURS", "12"))
# Productionda HTTPS orqali ishga tushirilganda .env'da SESSION_COOKIE_SECURE=true qiling.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
