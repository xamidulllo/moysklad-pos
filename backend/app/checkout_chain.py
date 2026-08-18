"""Checkout uchun MoySklad hujjat zanjirini yaratadi: Заказ покупателя ->
Otgruzka -> To'lov (cashin/paymentin).

Bu — avval main.py'dagi POST /api/checkout marshrutining tanasi edi (xatti-
harakati o'zgarishsiz ko'chirilgan). Endi alohida modulga chiqarilgan, chunki
ikkita chaqiruvchi bor: main.py'ning o'zi (to'g'ridan-to'g'ri checkout,
Avtoservis'dan boshqa MoySklad hisoblari uchun) va sync_job.py (Google
Sheets'da navbatga qo'yilgan buyurtmalarni MoySklad'ga ko'chiradigan fon
vazifasi). sync_job.py main.py'ni import qila olmaydi (aylanma import bo'lar
edi), shuning uchun bu mantiq mustaqil modulda turadi.
"""
from fastapi import HTTPException

from .cache import _cached
from .moysklad_client import ms_request
from .schemas import CheckoutRequest
from .utils import _id_from_href, _to_minor_units


class RollbackFailedError(Exception):
    """execute_checkout_chain() MoySklad'da xato uchragach o'zi yaratgan
    hujjatlarni orqaga qaytarishga (DELETE) urinadi — agar shu DELETE so'rovi
    o'zi ham muvaffaqiyatsiz tugasa, MoySklad'da "osilib qolgan" hujjat
    qolishi mumkin. Bu holatni oddiy HTTPException'dan ajratib ko'rsatish
    uchun alohida exception turi: fon sinxronizatsiya vazifasi (sync_job.py)
    buni ko'rib, qatorni "needs_manual_check" holatiga o'tkazishi kerak —
    oddiy "failed" holatiga emas, chunki MoySklad'dagi haqiqiy holat noaniq
    qolgan."""


_YES_LIKE_NAMES = ("ha", "да", "yes", "true", "тўғри", "to'g'ri")

_POS_SALES_CHANNEL_NAME = "POS Mini App"


async def _get_default_currency_id(token: str) -> "str | None":
    """Tashkilotning bazaviy (учетная) valyutasi ID'sini qaytaradi.

    MoySklad qo'lda kiritilgan kursni ("rate.value") faqat bazaviy valyutadan
    FARQLI valyutadagi to'lovlar uchun qabul qiladi — aks holda xato 3007
    ("Нельзя задать курс валюты учета, отличный от 1") qaytaradi. Shu sabab
    checkout paytida tanlangan hisob valyutasini shu bilan solishtiramiz.
    """

    async def loader():
        data = await ms_request("GET", "/entity/currency", token=token, params={"limit": 100})
        default_row = next((r for r in data.get("rows", []) if r.get("default")), None)
        return {"id": default_row["id"] if default_row else None}

    result = await _cached("default_currency", token, loader)
    return result["id"]


async def _get_pos_sales_channel_meta(token: str) -> dict:
    """Shu ilova orqali yaratilgan buyurtmalarni MoySklad'dagi boshqa botlar/qo'lda
    kiritilgan buyurtmalardan ajratib turish uchun barcha buyurtmalarga bitta
    maxsus "Канал продаж" (sales channel) biriktiriladi. Birinchi chaqiruvda
    shu nomdagi kanal qidiriladi, topilmasa avtomatik yaratiladi (real API'da
    tekshirilgan — "type": "ECOMMERCE" bilan)."""

    async def loader():
        data = await ms_request("GET", "/entity/saleschannel", token=token, params={"limit": 100})
        existing = next(
            (r for r in data.get("rows", []) if r.get("name") == _POS_SALES_CHANNEL_NAME), None
        )
        if existing:
            return {"meta": existing["meta"]}
        created = await ms_request(
            "POST",
            "/entity/saleschannel",
            token=token,
            json={"name": _POS_SALES_CHANNEL_NAME, "type": "ECOMMERCE"},
        )
        return {"meta": created["meta"]}

    result = await _cached("pos_sales_channel", token, loader)
    return result["meta"]


async def _get_required_order_attributes(token: str) -> list:
    """Ba'zi MoySklad hisoblarida buyurtma (customerorder) uchun qo'shimcha
    maydonlar (custom attributes) "majburiy" deb belgilangan bo'ladi — bunday
    hisoblarda ularsiz order yaratib bo'lmaydi (MoySklad butunlay rad etadi).
    Bu funksiya har doim shu hisobning HAQIQIY majburiy maydonlarini o'zi
    aniqlab, ularga mantiqiy standart qiymat beradi — hech qanday maydon nomi
    yoki ID kodda qattiq yozilmagan, shuning uchun boshqa MoySklad hisobida
    boshqa (yoki hech qanday) majburiy maydon bo'lsa ham avtomatik moslashadi.

    Faqat ikki turdagi maydon uchun ishonchli standart tanlanadi:
      - "boolean" — True qilib qo'yiladi;
      - "customentity" (lug'atdan tanlash, masalan "Ha"/"Yo'q") — "Ha"ga
        o'xshash nomli variant, aks holda yagona/birinchi variant tanlanadi.
    Boshqa turdagi (matn, sana va h.k.) majburiy maydonlar uchun ishonchli
    standart yo'q — ular o'tkazib yuboriladi (kerak bo'lsa administrator shu
    maydonni "majburiy emas" qilishi kerak bo'ladi).
    """

    async def loader():
        try:
            data = await ms_request(
                "GET", "/entity/customerorder/metadata/attributes", token=token, params={"limit": 1000}
            )
        except HTTPException:
            # Ba'zi MoySklad tariflarida qo'shimcha maydonlar funksiyasi umuman
            # yo'q — bunday hisoblarda so'rovning o'zi xato qaytaradi (masalan
            # "Тарифное ограничение"). Bu checkout'ni butunlay to'xtatmasligi
            # kerak — shunchaki majburiy maydon yo'q deb hisoblanadi.
            return {"items": []}
        items = []
        for attr in data.get("rows", []):
            if not attr.get("required"):
                continue
            attr_meta = attr["meta"]

            if attr.get("type") == "boolean":
                items.append({"meta": attr_meta, "value": True})
                continue

            if attr.get("type") == "customentity":
                custom_entity_href = (attr.get("customEntityMeta") or {}).get("href", "")
                custom_entity_id = _id_from_href(custom_entity_href) if custom_entity_href else None
                if not custom_entity_id:
                    continue
                try:
                    dict_data = await ms_request(
                        "GET", f"/entity/customentity/{custom_entity_id}", token=token, params={"limit": 100}
                    )
                except HTTPException:
                    continue
                options = dict_data.get("rows", [])
                if not options:
                    continue
                chosen = next(
                    (o for o in options if o.get("name", "").strip().lower() in _YES_LIKE_NAMES),
                    options[0],
                )
                items.append({"meta": attr_meta, "value": {"meta": chosen["meta"], "name": chosen.get("name")}})

        return {"items": items}

    result = await _cached("required_order_attributes", token, loader)
    return result["items"]


async def execute_checkout_chain(payload: CheckoutRequest, token: str, external_code: "str | None" = None) -> dict:
    """Заказ покупателя -> Otgruzka -> To'lov zanjirini yaratadi.

    `external_code` berilsa (Sheets navbatidan sinxronlanayotgan buyurtmaning
    order_id'si), buyurtmaga MoySklad "externalCode" sifatida yoziladi — bu
    MoySklad hujjatidan orqaga, Sheets qatoriga qaytadigan barqaror havola,
    "needs_manual_check" holatlarini qo'lda tekshirish uchun ishlatiladi.

    Rollback (yaratilgan hujjatlarni DELETE qilish) muvaffaqiyatsiz tugasa
    (masalan tarmoq uzilishi), oddiy HTTPException emas, RollbackFailedError
    ko'tariladi — chunki bu holda MoySklad'dagi haqiqiy holat noaniq qoladi
    va chaqiruvchi buni oddiy "xato" dan farqlab, alohida ko'rib chiqishi
    kerak.
    """
    if not payload.items:
        raise HTTPException(status_code=400, detail="Savat bo'sh")

    # MUHIM (real API'da to'g'ridan-to'g'ri tekshirilgan): "positions[].price"
    # hujjatning O'Z rate.currency birligida ishlatiladi — MoySklad uni bazaviy
    # valyutaga hech qanday avtomatik konvertatsiya qilmaydi. Shu sabab frontend
    # narxni to'g'ridan-to'g'ri kassir tanlagan valyutada yuboradi, bu yerda
    # hech qanday qo'shimcha konvertatsiya qilinmaydi.
    positions = [
        {
            "quantity": item.quantity,
            "price": _to_minor_units(item.price),
            "assortment": {"meta": item.assortment_meta},
        }
        for item in payload.items
    ]

    # Kassir chet el valyutasini tanlagan bo'lsa, shu bitta "rate" BARCHA UCH
    # hujjatga (buyurtma, otgruzka, to'lov) baravar qo'llaniladi — aks holda
    # ular turli valyutada chiqib, chalkashlik yuzaga kelardi (haqiqiy
    # foydalanishda aynan shu xato tasdiqlangan edi). MUHIM: MoySklad
    # tashkilotning bazaviy (учетная) valyutasi uchun rate.value != 1
    # yuborilsa xato 3007 bilan rad etadi — kurs faqat CHET EL valyutasidagi
    # hisoblarda qo'llaniladi.
    document_rate = None
    if payload.exchange_rate and payload.exchange_rate > 0 and payload.currency_meta:
        selected_currency_id = _id_from_href(payload.currency_meta.get("href", ""))
        default_currency_id = await _get_default_currency_id(token)
        if selected_currency_id != default_currency_id:
            # MUHIM (MoySklad interfeysida vizual tasdiqlangan): API'ning
            # "rate.value" maydoni kassir kiritgan "1 bazaviy = X hujjat valyutasi"
            # (masalan "1 dollar = 12000 so'm") yo'nalishining TESKARISIDA
            # saqlanadi. MoySklad o'z interfeysida "1 USD = 12 000 UZS" to'g'ri
            # ko'rsatishi uchun bu yerga 1/12000 yuborilishi kerak — kassir
            # kiritgan raqamning o'zi emas.
            document_rate = {
                "value": 1 / payload.exchange_rate,
                "currency": {"meta": payload.currency_meta},
            }

    # 1) Заказ покупателя (customerorder) — POS-sotuvning boshlang'ich hujjati.
    # Otgruzka va to'lov ikkalasi ham shunga bog'lanadi (pastga qarang).
    order_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "store": {"meta": payload.store_meta},
        "positions": positions,
        "applicable": True,
        # Shu ilova orqali yaratilgan buyurtmalarni boshqa botlar/qo'lda
        # kiritilganlardan ajratish uchun — "Tarix" bo'limi shu kanal bo'yicha
        # filtrlaydi (real API'da tekshirilgan).
        "salesChannel": {"meta": await _get_pos_sales_channel_meta(token)},
    }
    if document_rate:
        order_body["rate"] = document_rate
    if payload.comment:
        order_body["description"] = payload.comment
    if external_code:
        order_body["externalCode"] = external_code

    # Ba'zi hisoblarda buyurtma uchun majburiy qo'shimcha maydonlar (custom
    # attributes) sozlangan bo'ladi — ularsiz MoySklad order yaratishni rad
    # etadi. Bu yerda ular avtomatik topilib, mantiqiy standart qiymat bilan
    # to'ldiriladi (real hisobda tekshirilgan).
    required_attrs = await _get_required_order_attributes(token)
    if required_attrs:
        order_body["attributes"] = required_attrs
    if payload.project_meta:
        order_body["project"] = {"meta": payload.project_meta}
    order = await ms_request("POST", "/entity/customerorder", token=token, json=order_body)
    order_sum = order.get("sum", 0)

    # 2) Otgruzka (demand) — buyurtmadan yaratiladi, "customerOrder" maydoni orqali
    # unga bog'lanadi (real API'dagi mavjud hujjatlarda tekshirilgan maydon nomi).
    demand_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "store": {"meta": payload.store_meta},
        "positions": positions,
        "applicable": True,
        "customerOrder": {"meta": order["meta"]},
    }
    if document_rate:
        demand_body["rate"] = document_rate
    if payload.comment:
        demand_body["description"] = payload.comment
    if payload.project_meta:
        demand_body["project"] = {"meta": payload.project_meta}
    try:
        demand = await ms_request("POST", "/entity/demand", token=token, json=demand_body)
    except HTTPException:
        # Otgruzka yaratilmasa, endi hech narsaga bog'lanmagan buyurtma qolib ketmasin.
        try:
            await ms_request("DELETE", f"/entity/customerorder/{order['id']}", token=token)
        except HTTPException as rollback_err:
            raise RollbackFailedError(
                f"Otgruzka yaratilmadi va buyurtma #{order.get('name')} ni orqaga qaytarib bo'lmadi: {rollback_err.detail}"
            ) from rollback_err
        raise

    # 3) To'lov hujjati — QARZGA sotuvda umuman yaratilmaydi (faqat buyurtma +
    # otgruzka qoladi, mijoz qarzi MoySklad'da to'lanmagan buyurtma sifatida ko'rinadi).
    if payload.is_debt:
        return {
            "order": {"id": order["id"], "name": order.get("name")},
            "demand": {"id": demand["id"], "name": demand.get("name")},
            "payment": None,
        }

    # Kassir tanlagan hujjat turiga qarab naqd (cashin) yoki bank (paymentin).
    # Foydalanuvchi tanloviga ko'ra BUYURTMAning o'ziga bog'lanadi
    # ("operations[].linkedSum"), otgruzkaga emas.
    # ESLATMA (real API'da tekshirilgan): "paymentin" hisob bog'lanishini
    # ("organizationAccount") to'liq saqlaydi, lekin "cashin" (ПКО) MoySklad'da
    # muayyan hisobga umuman bog'lanmaydi — u faqat tashkilotning umumiy kassa
    # balansini oshiradi, shuning uchun organizationAccount maydoni cashin uchun
    # jo'natilsa ham e'tiborga olinmaydi (xato bermaydi, shunchaki saqlanmaydi).
    payment_body = {
        "organization": {"meta": payload.organization_meta},
        "agent": {"meta": payload.agent_meta},
        "applicable": True,
        "sum": order_sum,
        "organizationAccount": {"meta": payload.account_meta},
        "operations": [{"meta": order["meta"], "linkedSum": order_sum}],
    }
    if document_rate:
        payment_body["rate"] = document_rate

    # Kartada oldindan to'langan holatlar uchun — to'lov sanasi/vaqti sotuv
    # vaqtidan farq qilishi mumkin, kassir buni qo'lda ko'rsata oladi
    # (real API'da tekshirilgan: "moment": "YYYY-MM-DD HH:MM:SS" formatida qabul qilinadi).
    if payload.payment_moment:
        payment_body["moment"] = payload.payment_moment

    payment_endpoint = "/entity/cashin" if payload.document_type == "cashin" else "/entity/paymentin"
    try:
        payment = await ms_request("POST", payment_endpoint, token=token, json=payment_body)
    except HTTPException:
        # To'lov yaratilmasa, endi bog'lanmagan otgruzka va buyurtma tizimda
        # "osilib qolmasligi" kerak — aks holda kassir xato ko'radi-yu, lekin
        # omborda tasdiqlangan otgruzka/buyurtma qolib ketadi.
        try:
            await ms_request("DELETE", f"/entity/demand/{demand['id']}", token=token)
            await ms_request("DELETE", f"/entity/customerorder/{order['id']}", token=token)
        except HTTPException as rollback_err:
            raise RollbackFailedError(
                f"To'lov yaratilmadi va otgruzka/buyurtma #{order.get('name')} ni orqaga qaytarib bo'lmadi: {rollback_err.detail}"
            ) from rollback_err
        raise

    return {
        "order": {"id": order["id"], "name": order.get("name")},
        "demand": {"id": demand["id"], "name": demand.get("name")},
        "payment": {"id": payment["id"], "name": payment.get("name")},
    }
