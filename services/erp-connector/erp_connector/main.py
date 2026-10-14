"""Connecteur ERP.

Dans la cible, ce service est un adaptateur entre l'ERP existant (conservé) et
le bus d'événements. Pour le prototype, il simule aussi l'ERP lui-même : stock
physique, lots en production, liste de préparation et bons de livraison.

* consomme ``order.confirmed`` et ``allocation.updated`` (status = reserved)
  pour alimenter la liste de préparation ;
* publie ``shipment.dispatched`` à l'expédition, ``stock.incoming`` à l'annonce
  d'un lot et ``stock.received`` à sa réception.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, Float, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import (
    ALLOCATION_UPDATED,
    ORDER_CONFIRMED,
    SHIPMENT_DISPATCHED,
    STOCK_INCOMING,
    STOCK_RECEIVED,
    AllocationUpdated,
    Event,
    OrderPayload,
    ShipmentDispatched,
    ShipmentLine,
    ShopRef,
    StockIncoming,
    StockReceived,
)
from surfwear_common.service import env, setup_logging

SERVICE = "erp-connector"
log = setup_logging(SERVICE)


# --------------------------------------------------------------------------- #
# Modèle de l'ERP simulé                                                       #
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    pass


class ErpProduct(Base):
    __tablename__ = "erp_products"
    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    unit_price: Mapped[float] = mapped_column(Float)
    on_hand: Mapped[int] = mapped_column(Integer)


class ErpLot(Base):
    __tablename__ = "erp_lots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(Integer)
    eta: Mapped[date] = mapped_column(Date)
    received: Mapped[bool] = mapped_column(Boolean, default=False)


class ErpOrder(Base):
    __tablename__ = "erp_orders"
    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shop: Mapped[dict] = mapped_column(JSON)


class Picking(Base):
    __tablename__ = "pickings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="to_pick")
    shipment_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(40))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lines: Mapped[list] = mapped_column(JSON)


class LotIn(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100_000)
    eta: date


class ReceptionIn(BaseModel):
    lot_id: str | None = None
    sku: str | None = None
    qty: int | None = Field(None, gt=0, le=100_000)

    @model_validator(mode="after")
    def check(self):
        if not self.lot_id and not (self.sku and self.qty):
            raise ValueError("Indiquer lot_id, ou sku et qty pour une réception sans annonce")
        return self


class DispatchIn(BaseModel):
    order_ids: list[str] | None = Field(None, description="Vide = expédier toute la liste de préparation")


def create_app(database_url: str | None = None, bus: EventBus | None = None,
               catalog_file: str | None = None) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./erp.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    catalog_path = Path(catalog_file or env("CATALOG_FILE", "/config/catalog.json"))

    async def seed() -> None:
        async with Session.begin() as s:
            if (await s.execute(select(func.count()).select_from(ErpProduct))).scalar_one():
                return
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            for p in data["products"]:
                s.add(ErpProduct(**p))
            for lot in data["lots"]:
                s.add(ErpLot(id=lot["id"], sku=lot["sku"], qty=lot["qty"], eta=date.fromisoformat(lot["eta"])))
        log.info("ERP simulé initialisé depuis %s", catalog_path)

    async def on_order_confirmed(event: Event) -> None:
        order = OrderPayload(**event.data)
        async with Session.begin() as s:
            if await s.get(ErpOrder, order.order_id) is None:
                s.add(ErpOrder(order_id=order.order_id, shop=order.shop.model_dump()))
            for line in order.lines:
                if line.qty_reserved > 0:
                    s.add(Picking(order_id=order.order_id, sku=line.sku, qty=line.qty_reserved))

    async def on_allocation(event: Event) -> None:
        a = AllocationUpdated(**event.data)
        if a.status != "reserved":
            return
        async with Session.begin() as s:
            if await s.get(ErpOrder, a.order_id) is None:
                return  # commande pas encore finalisée : rien à préparer
            s.add(Picking(order_id=a.order_id, sku=a.sku, qty=a.qty))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        await seed()
        bus.subscribe(f"{SERVICE}.orders", [ORDER_CONFIRMED], on_order_confirmed)
        bus.subscribe(f"{SERVICE}.allocations", [ALLOCATION_UPDATED], on_allocation)
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), 
        title="Connecteur ERP (ERP simulé)",
        version="1.0.0",
        description="Pont entre l'ERP de l'entrepôt et le bus d'événements ; écran du logisticien.",
        lifespan=lifespan,
    )

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/erp/stock", tags=["ERP"], summary="Stock physique et lots en production")
    async def get_stock():
        async with Session() as s:
            products = (await s.execute(select(ErpProduct).order_by(ErpProduct.sku))).scalars().all()
            lots = (await s.execute(select(ErpLot).where(ErpLot.received.is_(False)).order_by(ErpLot.eta))).scalars().all()
        return {
            "products": [
                {"sku": p.sku, "name": p.name, "category": p.category, "unit_price": p.unit_price, "on_hand": p.on_hand}
                for p in products
            ],
            "lots": [{"id": l.id, "sku": l.sku, "qty": l.qty, "eta": l.eta.isoformat()} for l in lots],
        }

    @app.get("/erp/pickings", tags=["Logistique"], summary="Liste de préparation")
    async def get_pickings():
        async with Session() as s:
            rows = (await s.execute(select(Picking).where(Picking.status == "to_pick").order_by(Picking.id))).scalars().all()
            orders = {o.order_id: o.shop for o in (await s.execute(select(ErpOrder))).scalars()}
            names = {p.sku: p.name for p in (await s.execute(select(ErpProduct))).scalars()}
        grouped: "OrderedDict[str, dict]" = OrderedDict()
        for p in rows:
            g = grouped.setdefault(p.order_id, {"order_id": p.order_id, "shop": orders.get(p.order_id), "lines": {}})
            line = g["lines"].setdefault(p.sku, {"sku": p.sku, "name": names.get(p.sku, p.sku), "qty": 0})
            line["qty"] += p.qty
        return [{**g, "lines": list(g["lines"].values())} for g in grouped.values()]

    @app.post("/erp/pickings/dispatch", tags=["Logistique"], summary="Expédier les colis préparés")
    async def dispatch(body: DispatchIn | None = None):
        order_ids = body.order_ids if body else None
        events: list[ShipmentDispatched] = []
        async with Session.begin() as s:
            q = select(Picking).where(Picking.status == "to_pick").order_by(Picking.id)
            if order_ids:
                q = q.where(Picking.order_id.in_(order_ids))
            rows = (await s.execute(q)).scalars().all()
            by_order: "OrderedDict[str, list[Picking]]" = OrderedDict()
            for p in rows:
                by_order.setdefault(p.order_id, []).append(p)
            for order_id, picks in by_order.items():
                erp_order = await s.get(ErpOrder, order_id)
                if erp_order is None:
                    continue  # commande pas encore connue de l'ERP
                now = datetime.now(timezone.utc)
                shipment_id = f"BL-{now:%Y%m%d}-{uuid4().hex[:6].upper()}"
                qty_by_sku: "OrderedDict[str, int]" = OrderedDict()
                for p in picks:
                    qty_by_sku[p.sku] = qty_by_sku.get(p.sku, 0) + p.qty
                    p.status, p.shipment_id = "shipped", shipment_id
                lines = []
                for sku, qty in qty_by_sku.items():
                    product = await s.get(ErpProduct, sku)
                    product.on_hand = max(product.on_hand - qty, 0)
                    lines.append(ShipmentLine(sku=sku, name=product.name, qty=qty))
                s.add(Shipment(id=shipment_id, order_id=order_id, dispatched_at=now,
                               lines=[l.model_dump() for l in lines]))
                events.append(ShipmentDispatched(shipment_id=shipment_id, order_id=order_id,
                                                 shop=ShopRef(**erp_order.shop), dispatched_at=now, lines=lines))
        for e in events:
            await bus.publish(Event.build(SHIPMENT_DISPATCHED, SERVICE, e))
        return [e.model_dump(mode="json") for e in events]

    @app.get("/erp/shipments", tags=["Logistique"], summary="Bons de livraison émis")
    async def get_shipments():
        async with Session() as s:
            rows = (await s.execute(select(Shipment).order_by(Shipment.dispatched_at.desc()))).scalars().all()
        return [{"id": r.id, "order_id": r.order_id, "dispatched_at": r.dispatched_at, "lines": r.lines} for r in rows]

    @app.post("/erp/lots", tags=["Production"], status_code=201, summary="Annoncer un lot en production")
    async def create_lot(body: LotIn):
        lot_id = f"LOT-{uuid4().hex[:6].upper()}"
        async with Session.begin() as s:
            if await s.get(ErpProduct, body.sku) is None:
                raise HTTPException(404, f"Référence inconnue : {body.sku}")
            s.add(ErpLot(id=lot_id, sku=body.sku, qty=body.qty, eta=body.eta))
        await bus.publish(Event.build(STOCK_INCOMING, SERVICE,
                                      StockIncoming(lot_id=lot_id, sku=body.sku, qty=body.qty, eta=body.eta)))
        return {"id": lot_id, "sku": body.sku, "qty": body.qty, "eta": body.eta}

    @app.post("/erp/receptions", tags=["Production"], summary="Réceptionner un lot en entrepôt")
    async def receive(body: ReceptionIn):
        async with Session.begin() as s:
            if body.lot_id:
                lot = await s.get(ErpLot, body.lot_id)
                if lot is None:
                    raise HTTPException(404, f"Lot inconnu : {body.lot_id}")
                if lot.received:
                    raise HTTPException(409, f"Lot déjà réceptionné : {body.lot_id}")
                qty = body.qty or lot.qty
            else:
                if await s.get(ErpProduct, body.sku) is None:
                    raise HTTPException(404, f"Référence inconnue : {body.sku}")
                lot = ErpLot(id=f"LOT-{uuid4().hex[:6].upper()}", sku=body.sku, qty=body.qty, eta=date.today())
                s.add(lot)
                qty = body.qty
            lot.received = True
            product = await s.get(ErpProduct, lot.sku)
            product.on_hand += qty
            payload = StockReceived(lot_id=lot.id, sku=lot.sku, qty=qty)
        await bus.publish(Event.build(STOCK_RECEIVED, SERVICE, payload))
        return payload.model_dump()

    return app
