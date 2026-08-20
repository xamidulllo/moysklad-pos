"""Oddiy xotiradagi kesh: hisoblar/tashkilotlar/omborlar/valyuta kabi ma'lumotlar
har so'rovda o'zgarmaydi, shuning uchun har bir ekranga kirishda MoySklad'ga
urilmaymiz. Kalitga token qo'shilgan — turli kassirlar (turli MoySklad
hisoblari/huquqlari) bir-birining keshlangan ma'lumotini ko'rmasligi uchun.

main.py (katalog marshrutlari) va checkout_chain.py (valyuta/sotuv kanali/
majburiy atribute qidiruvlari) ikkalasi ham shu keshni ishlatadi, shuning uchun
aylanma import bo'lmasligi uchun alohida modulga chiqarilgan.
"""
import time
from typing import Awaitable, Callable

_cache: dict[str, tuple[float, dict]] = {}
# Hisoblar/tashkilotlar/valyutalar/majburiy atributlar kabi ma'lumotlar juda
# kamdan-kam o'zgaradi — shuning uchun 1 soatga keshlanadi (avval 60s edi).
CACHE_TTL_SECONDS = 3600


async def _cached(key: str, token: str, loader: Callable[[], Awaitable[dict]]) -> dict:
    cache_key = f"{key}:{token}"
    now = time.time()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = await loader()
    _cache[cache_key] = (now, value)
    return value
