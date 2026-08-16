from typing import Optional

from pydantic import BaseModel, Field, model_validator


class LoginRequest(BaseModel):
    login: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CartItemIn(BaseModel):
    assortment_meta: dict = Field(..., description="MoySklad mahsulot/modifikatsiya meta obyekti")
    quantity: float = Field(..., gt=0)
    price: float = Field(..., ge=0, description="Bir dona narxi so'mda (tiyinda emas)")


class CheckoutRequest(BaseModel):
    organization_meta: dict
    store_meta: dict
    agent_meta: dict
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

    @model_validator(mode="after")
    def _require_payment_fields_unless_debt(self):
        if not self.is_debt and (not self.account_meta or not self.document_type):
            raise ValueError("account_meta va document_type qarzga bo'lmagan sotuv uchun majburiy")
        return self


class CounterpartyCreate(BaseModel):
    name: str = Field(..., min_length=1)
    phone: Optional[str] = None
