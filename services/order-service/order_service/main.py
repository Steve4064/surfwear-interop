"""Service Commandes : prise de commande en ligne, suivi, registre des boutiques."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import (
    ALLOCATION_UPDATED,
    ORDER_CONFIRMED,
    ORDER_UPDATED,
    SHIPMENT_DISPATCHED,
    AllocationUpdated,
    Event,
    OrderLinePayload,
    OrderPayload,
    ScheduledQty,
    ShipmentDispatched,
    ShopRef,
)
from surfwear_common.service import env, setup_logging

from .models import Base, Order, OrderLine, Shop

SERVICE = "order-service"
log = setup_logging(SERVICE)

STATUS_LABELS = {
    "confirmed": "Confirmée, entièrement réservée",
    "backordered": "Confirmée avec reliquat",
    "partially_shipped": "Partiellement expédiée",
    "shipped": "Expédiée",
}


class LineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(gt=0, le=10_000)


class OrderIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=200)


# --------------------------------------------------------------------------- #
# Projection d'une commande                                                    #
# --------------------------------------------------------------------------- #
def order_status(order: Order) -> str:
    ordered = sum(l.qty_ordered for l in order.lines)
    shipped = sum(l.qty_shipped for l in order.lines)
    if shipped and shipped >= ordered:
        return "shipped"
    if shipped:
        return "partially_shipped"
    if all(l.qty_reserved == l.qty_ordered for l in order.lines):
        return "confirmed"
    return "backordered"


def to_payload(order: Order, change: dict | None = None) -> OrderPayload:
    return OrderPayload(
        order_id=order.id,
        shop=ShopRef(id=order.shop.id, name=order.shop.name, connector=order.shop.connector, email=order.shop.email),
        status=order_status(order),
        created_at=order.created_at,
        total_amount=round(sum(l.unit_price * l.qty_ordered for l in order.lines), 2),
        lines=[
            OrderLinePayload(
                sku=l.sku, name=l.name, unit_price=l.unit_price, qty_ordered=l.qty_ordered,
                qty_reserved=l.qty_reserved, scheduled=[ScheduledQty(**x) for x in l.scheduled],
                qty_unscheduled=l.qty_unscheduled, qty_shipped=l.qty_shipped,
            )
            for l in order.lines
        ],
        change=change,
    )


def to_dto(order: Order) -> dict:
    data = to_payload(order).model_dump(mode="json")
    data["status_label"] = STATUS_LABELS[data["status"]]
    return data


def _take_scheduled(line: OrderLine, lot_id: str | None, qty: int) -> None:
    """Retire ``qty`` unités des quantités planifiées (en priorité sur le lot indiqué)."""
    entries = sorted(line.scheduled, key=lambda x: (x.get("lot_id") != lot_id, x.get("eta") or ""))
    rest = qty
    kept = []
    for e in entries:
        t = min(e["qty"], rest)
        rest -= t
        if e["qty"] - t:
            kept.append({**e, "qty": e["qty"] - t})
    line.scheduled = sorted(kept, key=lambda x: x.get("eta") or "")
    flag_modified(line, "scheduled")


def apply_allocation(line: OrderLine, a: AllocationUpdated) -> None:
    if a.previous == "lot":
        _take_scheduled(line, a.lot_id, a.qty)
    elif a.previous == "unscheduled":
        line.qty_unscheduled -= a.qty
    if a.status == "reserved":
        line.qty_reserved += a.qty
    elif a.status == "scheduled":
        line.scheduled = sorted(
            line.scheduled + [{"qty": a.qty, "eta": a.eta.isoformat() if a.eta else None, "lot_id": a.lot_id}],
            key=lambda x: x.get("eta") or "",
        )
        flag_modified(line, "scheduled")
    elif a.status == "unscheduled":
        line.qty_unscheduled += a.qty


# --------------------------------------------------------------------------- #
# Application                                                                  #
# --------------------------------------------------------------------------- #
def create_app(
    database_url: str | None = None,
    bus: EventBus | None = None,
    stock_client: httpx.AsyncClient | None = None,
    shops_file: str | None = None,
) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./orders.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    stock = stock_client or httpx.AsyncClient(base_url=env("STOCK_URL", "http://stock-service:8000"), timeout=5)
    shops_path = Path(shops_file or env("SHOPS_FILE", "/config/shops.json"))

    async def seed_shops() -> None:
        async with Session.begin() as s:
            if (await s.execute(select(func.count()).select_from(Shop))).scalar_one():
                return
            if not shops_path.exists():
                log.warning("Fichier des boutiques introuvable : %s", shops_path)
                return
            for shop in json.loads(shops_path.read_text(encoding="utf-8"))["shops"]:
                s.add(Shop(**shop))
        log.info("Registre des boutiques initialisé depuis %s", shops_path)

    async def load(s, order_id: str) -> Order | None:
        return (
            await s.execute(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))
        ).unique().scalar_one_or_none()

    async def on_allocation(event: Event) -> None:
        a = AllocationUpdated(**event.data)
        async with Session.begin() as s:
            order = await load(s, a.order_id)
            if order is None:
                log.warning("Allocation pour une commande inconnue : %s", a.order_id)
                return
            line = next((l for l in order.lines if l.sku == a.sku), None)
            if line is None:
                return
            apply_allocation(line, a)
            order.updated_at = datetime.now(timezone.utc)
            await s.flush()
            payload = to_payload(order, change=a.model_dump(mode="json"))
        await bus.publish(Event.build(ORDER_UPDATED, SERVICE, payload))

    async def on_shipment(event: Event) -> None:
        sh = ShipmentDispatched(**event.data)
        async with Session.begin() as s:
            order = await load(s, sh.order_id)
            if order is None:
                return
            for sl in sh.lines:
                line = next((l for l in order.lines if l.sku == sl.sku), None)
                if line:
                    line.qty_reserved -= sl.qty
                    line.qty_shipped += sl.qty
            order.updated_at = datetime.now(timezone.utc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        await seed_shops()
        bus.subscribe(f"{SERVICE}.allocations", [ALLOCATION_UPDATED], on_allocation)
        bus.subscribe(f"{SERVICE}.shipments", [SHIPMENT_DISPATCHED], on_shipment)
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), 
        title="Service Commandes",
        version="1.0.0",
        description="Commande en ligne 24h/24 pour les boutiques (portail ou logiciel de l'éditeur), "
        "avec réservation immédiate du stock.",
        lifespan=lifespan,
    )

    async def current_shop(x_api_key: str = Header(..., description="Clé d'API de la boutique")) -> Shop:
        async with Session() as s:
            shop = (await s.execute(select(Shop).where(Shop.api_key == x_api_key))).scalar_one_or_none()
        if shop is None:
            raise HTTPException(401, "Clé d'API inconnue")
        return shop

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/api/me", tags=["boutiques"], summary="Boutique authentifiée")
    async def me(shop: Shop = Depends(current_shop)):
        return {"id": shop.id, "name": shop.name, "city": shop.city, "connector": shop.connector}

    @app.post("/api/orders", tags=["boutiques"], status_code=201, summary="Passer une commande")
    async def create_order(
        body: OrderIn,
        response: Response,
        shop: Shop = Depends(current_shop),
        idempotency_key: str | None = Header(None, max_length=80,
                                             description="Rejouer la même clé ne crée pas de doublon"),
    ):
        if idempotency_key:
            async with Session() as s:
                existing = (
                    await s.execute(
                        select(Order).where(Order.shop_id == shop.id, Order.idempotency_key == idempotency_key)
                    )
                ).unique().scalar_one_or_none()
            if existing:
                response.status_code = 200
                return to_dto(existing)

        order_id = f"CMD-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:6].upper()}"
        try:
            resp = await stock.post(
                "/internal/allocations",
                json={"order_id": order_id, "lines": [l.model_dump() for l in body.lines]},
            )
        except httpx.HTTPError as exc:
            log.error("Service Stock injoignable : %s", exc)
            raise HTTPException(503, "Service de stock momentanément indisponible, réessayez")
        if resp.status_code == 422:
            raise HTTPException(422, resp.json().get("detail"))
        resp.raise_for_status()

        try:
            async with Session.begin() as s:
                order = Order(id=order_id, shop_id=shop.id, idempotency_key=idempotency_key)
                s.add(order)
                for l in resp.json()["lines"]:
                    s.add(OrderLine(
                        order_id=order_id, sku=l["sku"], name=l["name"], unit_price=l["unit_price"],
                        qty_ordered=l["requested"], qty_reserved=l["reserved"],
                        scheduled=[{"qty": x["qty"], "eta": x["eta"], "lot_id": x["lot_id"]} for x in l["scheduled"]],
                        qty_unscheduled=l["unscheduled"],
                    ))
        except IntegrityError:  # même clé d'idempotence envoyée en parallèle
            async with Session() as s:
                existing = (
                    await s.execute(
                        select(Order).where(Order.shop_id == shop.id, Order.idempotency_key == idempotency_key)
                    )
                ).unique().scalar_one()
            response.status_code = 200
            return to_dto(existing)

        async with Session() as s:
            order = await load(s, order_id)
        await bus.publish(Event.build(ORDER_CONFIRMED, SERVICE, to_payload(order)))
        log.info("Commande %s confirmée pour %s", order_id, shop.id)
        return to_dto(order)

    @app.get("/api/orders", tags=["boutiques"], summary="Commandes de la boutique")
    async def list_orders(shop: Shop = Depends(current_shop)):
        async with Session() as s:
            orders = (
                await s.execute(select(Order).where(Order.shop_id == shop.id).order_by(Order.created_at.desc()))
            ).unique().scalars().all()
            return [to_dto(o) for o in orders]

    @app.get("/api/orders/{order_id}", tags=["boutiques"], summary="Détail d'une commande")
    async def get_order(order_id: str, shop: Shop = Depends(current_shop)):
        async with Session() as s:
            order = await load(s, order_id)
        if order is None or order.shop_id != shop.id:
            raise HTTPException(404, "Commande introuvable")
        return to_dto(order)

    return app
