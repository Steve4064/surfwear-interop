"""Service Stock : disponibilité, allocations sur stock réel et lots, reliquats."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import (
    ALLOCATION_UPDATED,
    SHIPMENT_DISPATCHED,
    STOCK_INCOMING,
    STOCK_RECEIVED,
    Event,
    ShipmentDispatched,
    StockIncoming,
    StockReceived,
)
from surfwear_common.service import env, setup_logging

from . import logic
from .models import Allocation, Base, Lot, Product

SERVICE = "stock-service"
log = setup_logging(SERVICE)


class LineIn(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=10_000)


class AllocationRequest(BaseModel):
    order_id: str
    lines: list[LineIn] = Field(min_length=1)


def create_app(
    database_url: str | None = None,
    bus: EventBus | None = None,
    erp_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./stock.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    erp = erp_client or httpx.AsyncClient(base_url=env("ERP_URL", "http://erp-connector:8000"), timeout=5)

    async def publish_all(events: list) -> None:
        for payload in events:
            await bus.publish(Event.build(ALLOCATION_UPDATED, SERVICE, payload))

    async def on_incoming(event: Event) -> None:
        async with Session.begin() as s:
            events = await logic.on_stock_incoming(s, StockIncoming(**event.data))
        await publish_all(events)

    async def on_received(event: Event) -> None:
        async with Session.begin() as s:
            events = await logic.on_stock_received(s, StockReceived(**event.data))
        await publish_all(events)

    async def on_shipment(event: Event) -> None:
        async with Session.begin() as s:
            await logic.on_shipment(s, ShipmentDispatched(**event.data))

    async def bootstrap(retries: int = 60) -> None:
        """Synchronisation initiale : copie le catalogue et les lots de l'ERP si la base est vide."""
        async with Session() as s:
            if (await s.execute(select(func.count()).select_from(Product))).scalar_one():
                return
        for attempt in range(1, retries + 1):
            try:
                resp = await erp.get("/erp/stock")
                resp.raise_for_status()
                break
            except Exception as exc:
                log.warning("ERP indisponible pour la synchronisation (%s/%s) : %s", attempt, retries, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError("Synchronisation initiale impossible")
        data = resp.json()
        async with Session.begin() as s:
            for p in data["products"]:
                s.add(Product(sku=p["sku"], name=p["name"], category=p["category"],
                              unit_price=p["unit_price"], on_hand=p["on_hand"], reserved=0))
            await s.flush()
            for lot in data["lots"]:
                s.add(Lot(id=lot["id"], sku=lot["sku"], qty=lot["qty"],
                          eta=date.fromisoformat(lot["eta"]), allocated=0))
        log.info("Synchronisation initiale : %s produits, %s lots", len(data["products"]), len(data["lots"]))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        bus.subscribe(f"{SERVICE}.stock", [STOCK_INCOMING, STOCK_RECEIVED],
                      lambda e: on_incoming(e) if e.type == STOCK_INCOMING else on_received(e))
        bus.subscribe(f"{SERVICE}.shipments", [SHIPMENT_DISPATCHED], on_shipment)
        await bus.connect()
        await bootstrap()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), 
        title="Service Stock",
        version="1.0.0",
        description="Disponibilité en temps réel, réservation atomique du stock, planification des reliquats.",
        lifespan=lifespan,
    )
    app.state.session = Session

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/api/catalog", tags=["boutiques"], summary="Catalogue et disponibilité")
    async def get_catalog():
        async with Session() as s:
            return await logic.catalog(s)

    @app.get("/api/availability/{sku}", tags=["boutiques"], summary="Disponibilité d'une quantité")
    async def get_availability(sku: str, qty: int = Query(1, gt=0, le=10_000)):
        async with Session() as s:
            try:
                return await logic.availability(s, sku, qty)
            except logic.UnknownSku:
                raise HTTPException(404, f"Référence inconnue : {sku}")

    @app.post("/internal/allocations", tags=["interne"], summary="Allouer une commande")
    async def post_allocations(req: AllocationRequest):
        try:
            async with Session.begin() as s:
                lines = await logic.allocate_order(s, req.order_id, [(l.sku, l.qty) for l in req.lines])
        except logic.UnknownSku as exc:
            raise HTTPException(422, {"message": "Références inconnues", "skus": exc.skus})
        return {"order_id": req.order_id, "lines": lines}

    @app.post("/internal/allocations/{order_id}/release", tags=["interne"],
              summary="Libérer une pré-réservation expirée ou annulée")
    async def release_allocations(order_id: str):
        async with Session.begin() as s:
            result = await logic.release_order(s, order_id)
        await publish_all(result["events"])
        return {"order_id": order_id, "released": result["released"],
                "reallocated": len(result["events"])}

    @app.get("/internal/allocations/{order_id}", tags=["interne"])
    async def get_allocations(order_id: str):
        async with Session() as s:
            rows = (await s.execute(select(Allocation).where(Allocation.order_id == order_id))).scalars()
            return [
                {"sku": a.sku, "qty": a.qty, "kind": a.kind, "lot_id": a.lot_id, "eta": a.eta}
                for a in rows
            ]

    return app
