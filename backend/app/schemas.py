from typing import Optional

from pydantic import BaseModel, Field


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
    account_meta: dict = Field(..., description="entity/organization/{id}/accounts dagi hisob meta'si")
    document_type: str = Field(..., pattern="^(cashin|paymentin)$")
    currency_meta: Optional[dict] = None
    exchange_rate: float = Field(1, ge=0)
