from typing import Optional

from pydantic import BaseModel, Field, model_validator


class CashierEntryRequest(BaseModel):
    """Endi shaxsiy MoySklad login/parol emas — faqat kassir o'z ismini
    kiritadi (hisobot/audit uchun), butun tizim bitta umumiy MoySklad
    hisobiga ulanadi."""
    name: str = Field(..., min_length=1, max_length=100)
    pin: Optional[str] = Field(None, max_length=32)


class CartItemIn(BaseModel):
    assortment_meta: dict = Field(..., description="MoySklad mahsulot/modifikatsiya meta obyekti")
    quantity: float = Field(..., gt=0)
    price: float = Field(..., ge=0, description="Bir dona narxi so'mda (tiyinda emas)")
    id: Optional[str] = Field(None, description="MoySklad assortment ID — Sheets navbati/qoldiq keshi uchun")
    name: Optional[str] = Field(None, description="Mahsulot nomi — Sheets qatorida/tahrirlash ekranida ko'rsatish uchun")


class CheckoutRequest(BaseModel):
    # Do'kon rejimida (config.SHOP_ORGANIZATION_ID sozlangan) bular endi
    # frontend'dan kutilmaydi — backend o'zi config'dagi qattiq belgilangan
    # tashkilot/kontragentni ishlatadi (main.py/shop_sync.py'ga qarang).
    # Boshqa (Do'kon bo'lmagan) joylashuv uchun hozirgidek majburiy bo'lib qoladi.
    organization_meta: Optional[dict] = None
    store_meta: dict
    agent_meta: Optional[dict] = None
    items: list[CartItemIn]
    is_debt: bool = Field(False, description="True bo'lsa — qarzga sotish, to'lov hujjati yaratilmaydi")
    account_meta: Optional[dict] = Field(None, description="entity/organization/{id}/accounts dagi hisob meta'si")
    document_type: Optional[str] = Field(None, pattern="^(cashin|paymentin)$")
    currency_meta: Optional[dict] = None
    exchange_rate: float = Field(1, ge=0)
    comment: Optional[str] = Field(None, max_length=2000)
    payment_moment: Optional[str] = Field(
        None, description="To'lov hujjati sanasi/vaqti, masalan '2026-08-01 09:30:00' (bo'sh bo'lsa — hozirgi vaqt)"
    )
    project_meta: Optional[dict] = Field(None, description="entity/project meta obyekti (ixtiyoriy)")

    # Do'kon rejimi: mijoz chet el valyutasida (masalan $) naqd bergan, lekin
    # sotuv summasidan ko'proq bo'lgani uchun mahalliy valyutada (so'm) qaytim
    # berilgan holat. Ikkalasi ham to'ldirilsa, shop_sync.py TO'LIQ
    # cash_given_amount'ni kirim (hisobga bog'langan) sifatida, cash_change_som
    # ni esa alohida, zakazga bog'lanmagan chiqim sifatida yozadi.
    cash_given_amount: Optional[float] = Field(None, ge=0, description="Mijoz qo'lga bergan naqd summa (o'z valyutasida)")
    cash_change_som: Optional[float] = Field(None, ge=0, description="Mijozga qaytarilgan qaytim, so'mda")

    # Faqat ko'rsatish uchun (flat) — checkout mantig'ida ISHLATILMAYDI, faqat
    # navbatga qo'yilganda Google Sheets qatoriga yozib qo'yish uchun, shunda
    # sync jarayoni bu qiymatlarni olish uchun qo'shimcha MoySklad so'rovi
    # yubormaydi (navbatga qo'yish MoySklad'ga umuman tegmasligi kerak).
    store_name: Optional[str] = None
    agent_name: Optional[str] = None
    currency_name: Optional[str] = None

    @model_validator(mode="after")
    def _require_payment_fields_unless_debt(self):
        if not self.is_debt and (not self.account_meta or not self.document_type):
            raise ValueError("account_meta va document_type qarzga bo'lmagan sotuv uchun majburiy")
        return self


class PendingOrderItemsEdit(BaseModel):
    """Hali sinxronlanmagan (Sheets navbatidagi) buyurtmaning miqdor/narxini
    tahrirlash — faqat items, boshqa maydonlar (mijoz/hisob/tashkilot)
    o'zgarmaydi (o'zgartirish kerak bo'lsa — o'chirib, qaytadan kiritiladi)."""

    items: list[CartItemIn] = Field(..., min_length=1)


class CounterpartyCreate(BaseModel):
    name: str = Field(..., min_length=1)
    phone: Optional[str] = None
