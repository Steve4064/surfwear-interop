"""Contrats d'échange asynchrones.

Chaque message publié sur le bus est une ``Event`` (enveloppe commune) dont le
champ ``data`` respecte l'un des schémas ci-dessous. La clé de routage AMQP est
le ``type`` de l'événement (ex. ``order.confirmed``).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

EXCHANGE = "surfwear.events"
DEAD_LETTER_EXCHANGE = "surfwear.dlx"
SCHEMA_VERSION = 1

# Types d'événements (= clés de routage)
ORDER_CONFIRMED = "order.confirmed"
ORDER_UPDATED = "order.updated"
ALLOCATION_UPDATED = "allocation.updated"
SHIPMENT_DISPATCHED = "shipment.dispatched"
STOCK_RECEIVED = "stock.received"
STOCK_INCOMING = "stock.incoming"
STOCK_ADJUSTED = "stock.adjusted"
PRODUCT_CREATED = "product.created"
PRODUCT_UPDATED = "product.updated"
INVOICE_ISSUED = "invoice.issued"
SHOP_REGISTERED = "shop.registered"
SHOP_APPROVED = "shop.approved"


class Event(BaseModel):
    """Enveloppe commune à tous les messages."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = SCHEMA_VERSION
    source: str
    data: dict

    @classmethod
    def build(cls, type_: str, source: str, payload: BaseModel | dict) -> "Event":
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        return cls(type=type_, source=source, data=data)


# --------------------------------------------------------------------------- #
# Schémas des données                                                          #
# --------------------------------------------------------------------------- #
class ShopRef(BaseModel):
    id: str
    name: str
    connector: Literal["rest", "csv", "none"] = "none"
    email: str


class ScheduledQty(BaseModel):
    qty: int
    eta: date | None = None
    lot_id: str | None = None


class OrderLinePayload(BaseModel):
    sku: str
    name: str
    unit_price: float
    qty_ordered: int
    qty_reserved: int
    scheduled: list[ScheduledQty] = []
    qty_unscheduled: int = 0
    qty_shipped: int = 0


class OrderPayload(BaseModel):
    order_id: str
    shop: ShopRef
    status: str
    created_at: datetime
    total_amount: float
    lines: list[OrderLinePayload]
    change: dict | None = None  # détail du changement pour order.updated


class AllocationUpdated(BaseModel):
    order_id: str
    sku: str
    qty: int
    status: Literal["scheduled", "reserved", "unscheduled"]
    previous: Literal["lot", "unscheduled", "stock"]
    eta: date | None = None
    lot_id: str | None = None


class ShipmentLine(BaseModel):
    sku: str
    name: str
    qty: int


class ShipmentDispatched(BaseModel):
    shipment_id: str  # numéro du bon de livraison
    order_id: str
    shop: ShopRef
    dispatched_at: datetime
    lines: list[ShipmentLine]


class StockReceived(BaseModel):
    lot_id: str
    sku: str
    qty: int


class StockIncoming(BaseModel):
    lot_id: str
    sku: str
    qty: int
    eta: date


class StockAdjusted(BaseModel):
    """Correction d'inventaire : nouveau stock physique constaté pour une référence."""
    sku: str
    on_hand: int
    delta: int
    reason: str = ""


class ProductPayload(BaseModel):
    sku: str
    name: str
    category: str
    unit_price: float
    vat_rate: float = 0.20
    description: str = ""
    active: bool = True


class InvoicePayload(BaseModel):
    invoice_id: str
    order_id: str
    shipment_id: str
    shop: ShopRef
    total_ht: float
    total_vat: float
    total_ttc: float
    due_date: date


class ShopRegistered(BaseModel):
    shop_id: str
    name: str
    city: str
    email: str
    status: str
