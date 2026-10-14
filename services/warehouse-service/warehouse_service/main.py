"""Service Entrepôt (ERP simulé + connecteur vers le bus).

Dans la cible, l'ERP existant reste maître du stock physique et ce service fait le
pont avec le bus. Pour le prototype, il simule aussi l'ERP.

Écrans de l'équipe Surfwear :
* préparation des colis (commandes finalisées) et expédition, avec bon de livraison ;
* réceptions de marchandise (lot annoncé ou réception libre), avec historique ;
* lots en production ;
* inventaire (correction du stock physique).

Événements publiés : shipment.dispatched, stock.incoming, stock.received, stock.adjusted.
Événements consommés : order.confirmed, allocation.updated, product.created.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import (
    ALLOCATION_UPDATED,
    ORDER_CONFIRMED,
    PRODUCT_CREATED,
    PRODUCT_UPDATED,
    SHIPMENT_DISPATCHED,
    STOCK_ADJUSTED,
    STOCK_INCOMING,
    STOCK_RECEIVED,
    AllocationUpdated,
    Event,
    OrderPayload,
    ShipmentDispatched,
    ShipmentLine,
    ShopRef,
    StockAdjusted,
    StockIncoming,
    StockReceived,
)
from surfwear_common.identity import Identity, require_staff
from surfwear_common.service import env, setup_logging

SERVICE = "warehouse-service"
log = setup_logging(SERVICE)


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
    shop_name: Mapped[str] = mapped_column(String(120), default="")
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_by: Mapped[str] = mapped_column(String(120), default="")
    lines: Mapped[list] = mapped_column(JSON)


class Reception(Base):
    """Historique des réceptions de marchandise."""
    __tablename__ = "receptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[str] = mapped_column(String(32))
    sku: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(Integer)
    reference: Mapped[str] = mapped_column(String(80), default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[str] = mapped_column(String(120), default="")


class Adjustment(Base):
    """Historique des corrections d'inventaire."""
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(32))
    before: Mapped[int] = mapped_column(Integer)
    after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    by: Mapped[str] = mapped_column(String(120), default="")


class LotIn(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=100_000)
    eta: date


class ReceptionIn(BaseModel):
    lot_id: str | None = Field(None, description="Lot annoncé à réceptionner")
    sku: str | None = Field(None, description="Réception libre : référence")
    qty: int | None = Field(None, gt=0, le=100_000, description="Quantité reçue (par défaut : celle du lot)")
    reference: str = Field("", max_length=80, description="N° de bon de livraison fournisseur")

    @model_validator(mode="after")
    def check(self):
        if not self.lot_id and not (self.sku and self.qty):
            raise ValueError("Indiquer un lot, ou une référence et une quantité")
        return self


class AdjustmentIn(BaseModel):
    sku: str
    on_hand: int = Field(ge=0, le=1_000_000, description="Stock physique compté")
    reason: str = Field("Inventaire", max_length=200)


class DispatchIn(BaseModel):
    order_ids: list[str] | None = Field(None, description="Vide = expédier toute la liste de préparation")


def now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(database_url: str | None = None, bus: EventBus | None = None,
               catalog_file: str | None = None) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./warehouse.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    catalog_path = Path(catalog_file or env("CATALOG_FILE", "/config/catalog.json"))

    async def seed() -> None:
        async with Session.begin() as s:
            if (await s.execute(select(func.count()).select_from(ErpProduct))).scalar_one():
                return
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            for p in data["products"]:
                s.add(ErpProduct(sku=p["sku"], name=p["name"], category=p["category"],
                                 unit_price=p["unit_price"], on_hand=p["on_hand"]))
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

    async def on_product(event: Event) -> None:
        d = event.data
        async with Session.begin() as s:
            p = await s.get(ErpProduct, d["sku"])
            if p is None:
                s.add(ErpProduct(sku=d["sku"], name=d["name"], category=d["category"],
                                 unit_price=d["unit_price"], on_hand=0))
            else:
                p.name, p.category, p.unit_price = d["name"], d["category"], d["unit_price"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        await seed()
        bus.subscribe(f"{SERVICE}.orders", [ORDER_CONFIRMED], on_order_confirmed)
        bus.subscribe(f"{SERVICE}.allocations", [ALLOCATION_UPDATED], on_allocation)
        bus.subscribe(f"{SERVICE}.products", [PRODUCT_CREATED, PRODUCT_UPDATED], on_product)
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Entrepôt", version="3.0.0",
                  description="Préparation, expédition, réceptions, lots et inventaire (ERP simulé).",
                  lifespan=lifespan)

    async def stock_snapshot() -> dict:
        async with Session() as s:
            products = (await s.execute(select(ErpProduct).order_by(ErpProduct.sku))).scalars().all()
            lots = (await s.execute(select(ErpLot).where(ErpLot.received.is_(False)).order_by(ErpLot.eta))).scalars().all()
        return {
            "products": [{"sku": p.sku, "name": p.name, "category": p.category, "unit_price": p.unit_price,
                          "on_hand": p.on_hand} for p in products],
            "lots": [{"id": l.id, "sku": l.sku, "qty": l.qty, "eta": l.eta.isoformat()} for l in lots],
        }

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/internal/stock", tags=["interne"], summary="Stock physique et lots (synchronisation initiale)")
    async def internal_stock():
        return await stock_snapshot()

    @app.get("/api/warehouse/stock", tags=["Entrepôt"], summary="Stock physique et lots en production")
    async def get_stock(_: Identity = Depends(require_staff)):
        return await stock_snapshot()

    # ------------------------------------------------------------ préparation et expédition
    @app.get("/api/warehouse/pickings", tags=["Entrepôt"], summary="Liste de préparation")
    async def get_pickings(_: Identity = Depends(require_staff)):
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

    @app.post("/api/warehouse/pickings/dispatch", tags=["Entrepôt"], summary="Expédier les colis préparés")
    async def dispatch(body: DispatchIn | None = None, who: Identity = Depends(require_staff)):
        order_ids = body.order_ids if body else None
        events: list[ShipmentDispatched] = []
        async with Session.begin() as s:
            q = select(Picking).where(Picking.status == "to_pick").order_by(Picking.id)
            if order_ids:
                q = q.where(Picking.order_id.in_(order_ids))
            by_order: "OrderedDict[str, list[Picking]]" = OrderedDict()
            for p in (await s.execute(q)).scalars().all():
                by_order.setdefault(p.order_id, []).append(p)
            for order_id, picks in by_order.items():
                erp_order = await s.get(ErpOrder, order_id)
                if erp_order is None:
                    continue
                t = now()
                shipment_id = f"BL-{t:%Y%m%d}-{uuid4().hex[:6].upper()}"
                qty_by_sku: "OrderedDict[str, int]" = OrderedDict()
                for p in picks:
                    qty_by_sku[p.sku] = qty_by_sku.get(p.sku, 0) + p.qty
                    p.status, p.shipment_id = "shipped", shipment_id
                lines = []
                for sku, qty in qty_by_sku.items():
                    product = await s.get(ErpProduct, sku)
                    product.on_hand = max(product.on_hand - qty, 0)
                    lines.append(ShipmentLine(sku=sku, name=product.name, qty=qty))
                shop = ShopRef(**erp_order.shop)
                s.add(Shipment(id=shipment_id, order_id=order_id, shop_name=shop.name, dispatched_at=t,
                               dispatched_by=who.name, lines=[l.model_dump() for l in lines]))
                events.append(ShipmentDispatched(shipment_id=shipment_id, order_id=order_id, shop=shop,
                                                 dispatched_at=t, lines=lines))
        for e in events:
            await bus.publish(Event.build(SHIPMENT_DISPATCHED, SERVICE, e))
        return [e.model_dump(mode="json") for e in events]

    @app.get("/api/warehouse/shipments", tags=["Entrepôt"], summary="Bons de livraison émis")
    async def get_shipments(_: Identity = Depends(require_staff)):
        async with Session() as s:
            rows = (await s.execute(select(Shipment).order_by(Shipment.dispatched_at.desc()))).scalars().all()
        return [{"id": r.id, "order_id": r.order_id, "shop_name": r.shop_name, "dispatched_at": r.dispatched_at,
                 "dispatched_by": r.dispatched_by, "lines": r.lines} for r in rows]

    # ------------------------------------------------------------ production et réceptions
    @app.post("/api/warehouse/lots", tags=["Entrepôt"], status_code=201, summary="Annoncer un lot en production")
    async def create_lot(body: LotIn, _: Identity = Depends(require_staff)):
        lot_id = f"LOT-{uuid4().hex[:6].upper()}"
        async with Session.begin() as s:
            if await s.get(ErpProduct, body.sku) is None:
                raise HTTPException(404, f"Référence inconnue : {body.sku}")
            s.add(ErpLot(id=lot_id, sku=body.sku, qty=body.qty, eta=body.eta))
        await bus.publish(Event.build(STOCK_INCOMING, SERVICE,
                                      StockIncoming(lot_id=lot_id, sku=body.sku, qty=body.qty, eta=body.eta)))
        return {"id": lot_id, "sku": body.sku, "qty": body.qty, "eta": body.eta}

    @app.post("/api/warehouse/receptions", tags=["Entrepôt"], summary="Réceptionner de la marchandise")
    async def receive(body: ReceptionIn, who: Identity = Depends(require_staff)):
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
                lot = ErpLot(id=f"REC-{uuid4().hex[:6].upper()}", sku=body.sku, qty=body.qty, eta=date.today())
                s.add(lot)
                qty = body.qty
            lot.received = True
            product = await s.get(ErpProduct, lot.sku)
            product.on_hand += qty
            s.add(Reception(lot_id=lot.id, sku=lot.sku, qty=qty, reference=body.reference,
                            received_at=now(), received_by=who.name))
            payload = StockReceived(lot_id=lot.id, sku=lot.sku, qty=qty)
        await bus.publish(Event.build(STOCK_RECEIVED, SERVICE, payload))
        return payload.model_dump()

    @app.get("/api/warehouse/receptions", tags=["Entrepôt"], summary="Historique des réceptions")
    async def list_receptions(_: Identity = Depends(require_staff)):
        async with Session() as s:
            rows = (await s.execute(select(Reception).order_by(Reception.received_at.desc()).limit(200))).scalars().all()
            names = {p.sku: p.name for p in (await s.execute(select(ErpProduct))).scalars()}
        return [{"lot_id": r.lot_id, "sku": r.sku, "name": names.get(r.sku, r.sku), "qty": r.qty,
                 "reference": r.reference, "received_at": r.received_at, "received_by": r.received_by} for r in rows]

    # ------------------------------------------------------------ inventaire
    @app.post("/api/warehouse/adjustments", tags=["Entrepôt"], summary="Corriger le stock après inventaire")
    async def adjust(body: AdjustmentIn, who: Identity = Depends(require_staff)):
        async with Session.begin() as s:
            p = await s.get(ErpProduct, body.sku)
            if p is None:
                raise HTTPException(404, f"Référence inconnue : {body.sku}")
            before, p.on_hand = p.on_hand, body.on_hand
            s.add(Adjustment(sku=p.sku, before=before, after=body.on_hand, reason=body.reason, at=now(), by=who.name))
        await bus.publish(Event.build(STOCK_ADJUSTED, SERVICE, StockAdjusted(
            sku=body.sku, on_hand=body.on_hand, delta=body.on_hand - before, reason=body.reason)))
        return {"sku": body.sku, "before": before, "after": body.on_hand}

    @app.get("/api/warehouse/adjustments", tags=["Entrepôt"], summary="Historique des inventaires")
    async def list_adjustments(_: Identity = Depends(require_staff)):
        async with Session() as s:
            rows = (await s.execute(select(Adjustment).order_by(Adjustment.at.desc()).limit(200))).scalars().all()
        return [{"sku": r.sku, "before": r.before, "after": r.after, "reason": r.reason, "at": r.at, "by": r.by}
                for r in rows]

    return app
