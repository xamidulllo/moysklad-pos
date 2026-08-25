"""Do'kon rejimi uchun "ish kuni" tushunchasi — yarim tun emas, har kuni aniq
bir soatda (standart 16:50, config.SHOP_DAY_CUTOFF) eski kun yopilib,
yangisi ochiladi. Masalan cutoff="16:50" bo'lsa: bugun 10:00dagi va bugun
16:49dagi sotuv BIR XIL ish kuniga tegishli; bugun 16:50dagi sotuv esa
ERTANGI ish kuniga tegishli bo'ladi.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

# Do'kon Toshkentda ishlaydi — "16:50" chegarasi shu yerlik vaqt bo'yicha
# tushuniladi (Apps Script trigger'lari ham "Asia/Tashkent" bo'yicha
# sozlangan, xuddi shu konventsiyaga mos).
_SHOP_TZ = ZoneInfo("Asia/Tashkent")


def _parse_cutoff(cutoff: str) -> time:
    hour_str, minute_str = cutoff.strip().split(":")
    return time(hour=int(hour_str), minute=int(minute_str))


def now_in_shop_tz() -> datetime:
    return datetime.now(_SHOP_TZ)


def business_day_key(moment: datetime, cutoff: str = "16:50") -> str:
    """`moment` qaysi ish kuniga tegishli ekanini "YYYY-MM-DD" ko'rinishida
    qaytaradi. Ish kuni [oldingi kun CUTOFF, shu kun CUTOFF) oralig'i sifatida
    belgilanadi — ya'ni CUTOFF vaqtining o'zi ALLAQACHON keyingi ish kuniga
    tegishli (chegara qat'iy: >= cutoff -> keyingi kun)."""
    cutoff_time = _parse_cutoff(cutoff)
    day = moment.date()
    if moment.time() >= cutoff_time:
        day = day.fromordinal(day.toordinal() + 1)
    return day.isoformat()
