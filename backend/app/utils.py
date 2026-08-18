"""Umumiy kichik yordamchi funksiyalar — main.py va checkout_chain.py ikkalasida
ham kerak, shuning uchun aylanma import (circular import) bo'lmasligi uchun
alohida modulga chiqarilgan."""


def _to_minor_units(sum_in_som: float) -> int:
    """MoySklad summalarni tiyinda (kopeykada) kutadi: 1 so'm = 100 birlik."""
    return round(sum_in_som * 100)


def _id_from_href(href: str) -> str:
    return href.rstrip("/").rsplit("/", 1)[-1]
