"""Hali MoySklad'ga sinxronlanmagan (Google Sheets navbatida turgan)
buyurtmalar tufayli vaqtincha kamaytirilishi kerak bo'lgan ombor qoldig'ini
xotirada saqlaydi.

Nega kerak: checkout endi Sheets navbatiga yozadi, MoySklad'ga darhol
yozmaydi (davriy sync buni keyinroq bajaradi). Agar mahsulot qidiruvi
MoySklad'ning "xom" (hali kamaytirilmagan) qoldig'ini ko'rsatib tursa,
navbatdagi bir nechta buyurtma bir xil oxirgi donani ikki marta sotib
yuborishi mumkin. Shu sabab har bir navbatga qo'yilgan buyurtma miqdori
darhol shu yerda "ayirilib" turadi, mahsulot qidiruvi buni MoySklad'ning
jonli qoldig'idan chegirib ko'rsatadi.

MUHIM: bu — hosila (derived), yo'q qilinishi mumkin bo'lgan proyeksiya.
Yagona ishonchli manba doim Google Sheets bo'lib qoladi (xuddi auth.py'dagi
_sessions kabi — bitta process xotirasida, server qayta ishga tushirilsa
yo'qoladi) — shuning uchun ilova ishga tushganda rebuild_from_rows() bilan
qayta tiklanadi (main.py'ning lifespan()'iga qarang), va bu faqat bitta
backend nusxasi ishlatilganda to'g'ri ishlaydi (Render bepul tarifi — bitta
instance, xuddi _sessions kabi)."""
import json
from typing import Iterable

from .utils import _id_from_href

_totals: dict[tuple[str, str], float] = {}
_order_items: dict[str, list[tuple[str, str, float]]] = {}


def _clear() -> None:
    _totals.clear()
    _order_items.clear()


def get_deduction(store_id: "str | None", assortment_id: "str | None") -> float:
    if not store_id or not assortment_id:
        return 0.0
    return _totals.get((store_id, assortment_id), 0.0)


def _apply(order_id: str, store_id: str, items: list[tuple[str, float]]) -> None:
    recorded = []
    for assortment_id, qty in items:
        if not assortment_id or qty <= 0:
            continue
        key = (store_id, assortment_id)
        _totals[key] = _totals.get(key, 0.0) + qty
        recorded.append((store_id, assortment_id, qty))
    if recorded:
        _order_items.setdefault(order_id, []).extend(recorded)


def apply_order(order_id: str, store_id: "str | None", items: list[tuple[str, float]]) -> None:
    """Yangi navbatga qo'yilgan buyurtma miqdorlarini qo'shadi. Buyurtma
    allaqachon qayd etilgan bo'lsa (tahrirlash), avval release_order()
    chaqirilishi kerak — aks holda eski+yangi miqdor ikki marta hisoblanadi."""
    if not store_id:
        return
    _apply(order_id, store_id, items)


def release_order(order_id: str) -> None:
    """Buyurtma sinxronlanganda (endi MoySklad'ning o'z qoldig'i buni hisobga
    oladi), bekor qilinganda yoki tahrirlashdan oldin eski miqdorni olib
    tashlash uchun."""
    for store_id, assortment_id, qty in _order_items.pop(order_id, []):
        key = (store_id, assortment_id)
        remaining = _totals.get(key, 0.0) - qty
        if remaining <= 1e-9:
            _totals.pop(key, None)
        else:
            _totals[key] = remaining


def _extract_items(payload: dict) -> list[tuple[str, float]]:
    items = []
    for item in payload.get("items", []):
        assortment_id = item.get("id") or _id_from_href((item.get("assortment_meta") or {}).get("href", ""))
        items.append((assortment_id, float(item.get("quantity") or 0)))
    return items


def rebuild_from_rows(rows: Iterable[dict]) -> None:
    """Server (qayta) ishga tushganda Sheets — yagona ishonchli manbadan —
    xotiradagi proyeksiyani to'liq qaytadan quradi."""
    _clear()
    from .sheets_client import PENDING_LIKE_STATUSES

    for row in rows:
        if row.get("status") not in PENDING_LIKE_STATUSES:
            continue
        store_id = row.get("store_id") or None
        if not store_id:
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            continue
        apply_order(row["order_id"], store_id, _extract_items(payload))
