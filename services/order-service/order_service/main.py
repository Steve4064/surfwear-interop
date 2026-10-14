"""Service Commandes : pré-réservation, finalisation, suivi des commandes.

L'identité (boutique, équipe Surfwear) est vérifiée par la passerelle et le service
Identité ; ce service la lit dans les en-têtes X-Auth-*.

Cycle d'une commande :
1. la boutique pré-réserve (POST /api/orders) : le stock est bloqué pendant HOLD_MINUTES ;
2. elle finalise (POST /api/orders/{id}/confirm) : la commande part à l'entrepôt
   et l'événement ``order.confirmed`` est publié ;
3. sinon, à l'échéance, la pré-réservation expire et le stock est rendu : il sert
   alors les reliquats des autres boutiques. La boutique peut aussi annuler.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
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
from surfwear_common.identity import Identity, require_shop, require_staff
from surfwear_common.service import env, setup_logging

from .models import Base, Order, OrderLine, utcnow_naive

SERVICE = "order-service"
log = setup_logging(SERVICE)

STATUS_LABELS = {
    "pending": "Pré-réservée, à finaliser",
    "expired": "Expirée, stock libéré",
    "cancelled": "Annulée",
    "confirmed": "Finalisée, entièrement réservée",
    "backordered": "Finalisée avec reliquat",
    "partially_shipped": "Partiellement expédiée",
    "shipped": "Expédiée",
}


class LineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    qty: int = Field(gt=0, le=10_000)


class OrderIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1, max_length=200)
    confirm: bool = Field(False, description="Finaliser immédiatement (clients techniques)")




# --------------------------------------------------------------------------- #
# Projection d'une commande                                                    #
# --------------------------------------------------------------------------- #
def order_status(order: Order) -> str:
    if order.state in ("pending", "expired", "cancelled"):
        return order.state
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
        shop=ShopRef(id=order.shop_id, name=order.shop_name, connector=order.shop_connector, email=order.shop_email),
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


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_dto(order: Order) -> dict:
    data = to_payload(order).model_dump(mode="json")
    data["created_at"] = iso_utc(order.created_at)
    data["state"] = order.state
    data["status_label"] = STATUS_LABELS[data["status"]]
    data["expires_at"] = iso_utc(order.expires_at) if order.state == "pending" else None
    data["seconds_left"] = (
        max(0, int((order.expires_at - utcnow_naive()).total_seconds()))
        if order.state == "pending" and order.expires_at else None
    )
    data["finalized_at"] = iso_utc(order.finalized_at)
    data["placed_by"] = order.placed_by
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
    identity_client: httpx.AsyncClient | None = None,
    hold_minutes: float | None = None,
    sweep_seconds: float | None = None,
) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./orders.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    stock = stock_client or httpx.AsyncClient(base_url=env("STOCK_URL", "http://stock-service:8000"), timeout=5)
    identity = identity_client or httpx.AsyncClient(base_url=env("IDENTITY_URL", "http://identity-service:8000"),
                                                    timeout=5)
    shop_cache: dict[str, tuple[float, dict]] = {}
    hold = timedelta(minutes=hold_minutes if hold_minutes is not None else float(env("HOLD_MINUTES", "15")))
    sweep_every = sweep_seconds if sweep_seconds is not None else float(env("SWEEP_SECONDS", "5"))

    # ------------------------------------------------------------ boutiques (service Identité)
    async def shop_info(shop_id: str) -> dict:
        hit = shop_cache.get(shop_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            r = await identity.get(f"/internal/shops/{shop_id}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Service Identité injoignable : %s", exc)
            raise HTTPException(503, "Service d'identité momentanément indisponible, réessayez")
        shop_cache[shop_id] = (time.monotonic() + 60, r.json())
        return r.json()

    async def load(s, order_id: str) -> Order | None:
        return (
            await s.execute(select(Order).where(Order.id == order_id).execution_options(populate_existing=True))
        ).unique().scalar_one_or_none()

    # ------------------------------------------------------------ libération du stock
    async def release(order_id: str) -> bool:
        try:
            r = await stock.post(f"/internal/allocations/{order_id}/release")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Libération de %s reportée : %s", order_id, exc)
            return False
        async with Session.begin() as s:
            await s.execute(update(Order).where(Order.id == order_id).values(released=True))
        log.info("Stock de %s libéré : %s", order_id, r.json().get("released"))
        return True

    async def sweep_once() -> int:
        """Fait expirer les pré-réservations échues et libère leur stock. Renvoie le nombre traité."""
        now = utcnow_naive()
        async with Session.begin() as s:
            await s.execute(
                update(Order)
                .where(Order.state == "pending", Order.expires_at <= now)
                .values(state="expired")
                .execution_options(synchronize_session=False)
            )
        async with Session() as s:
            todo = (
                await s.execute(
                    select(Order.id).where(Order.state.in_(["expired", "cancelled"]), Order.released.is_(False))
                )
            ).scalars().all()
        done = 0
        for order_id in todo:
            done += await release(order_id)
        return done

    async def sweeper() -> None:
        while True:
            try:
                await sweep_once()
            except Exception:
                log.exception("Erreur pendant le contrôle des expirations")
            await asyncio.sleep(sweep_every)

    # ------------------------------------------------------------ événements
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
            finalized = order.state == "confirmed"
        if finalized:  # les boutiques ne sont informées que des commandes finalisées
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
        bus.subscribe(f"{SERVICE}.allocations", [ALLOCATION_UPDATED], on_allocation)
        bus.subscribe(f"{SERVICE}.shipments", [SHIPMENT_DISPATCHED], on_shipment)
        await bus.connect()
        task = asyncio.create_task(sweeper()) if sweep_every > 0 else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await bus.close()
        await engine.dispose()

    app = FastAPI(
        root_path=env("ROOT_PATH", ""),
        title="Service Commandes",
        version="3.0.0",
        description="Pré-réservation du stock, finalisation et suivi des commandes.",
        lifespan=lifespan,
    )
    app.state.sweep_once = sweep_once
    app.state.session = Session

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    # ------------------------------------------------------------ boutiques
    async def place_order(body: "OrderIn", who: Identity, idempotency_key: str | None, response: Response) -> dict:
        shop_id = who.shop_id
        if idempotency_key:
            async with Session() as s:
                existing = (
                    await s.execute(
                        select(Order).where(Order.shop_id == shop_id, Order.idempotency_key == idempotency_key)
                    )
                ).unique().scalar_one_or_none()
            if existing:
                response.status_code = 200
                return to_dto(existing)

        shop = await shop_info(shop_id)
        if shop.get("status") != "active":
            raise HTTPException(403, "Compte boutique en attente de validation par Surfwear")
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
                s.add(Order(id=order_id, shop_id=shop_id, shop_name=shop["name"], shop_connector=shop["connector"],
                            shop_email=shop["email"], placed_by=who.name, idempotency_key=idempotency_key,
                            state="pending", expires_at=utcnow_naive() + hold))
                for l in resp.json()["lines"]:
                    s.add(OrderLine(
                        order_id=order_id, sku=l["sku"], name=l["name"], unit_price=l["unit_price"],
                        qty_ordered=l["requested"], qty_reserved=l["reserved"],
                        scheduled=[{"qty": x["qty"], "eta": x["eta"], "lot_id": x["lot_id"]} for x in l["scheduled"]],
                        qty_unscheduled=l["unscheduled"],
                    ))
        except IntegrityError:  # même clé d'idempotence envoyée en parallèle
            await release(order_id)
            async with Session() as s:
                existing = (
                    await s.execute(
                        select(Order).where(Order.shop_id == shop_id, Order.idempotency_key == idempotency_key)
                    )
                ).unique().scalar_one()
            response.status_code = 200
            return to_dto(existing)

        log.info("Commande %s pré-réservée pour %s", order_id, shop_id)
        if body.confirm:
            return await finalize(order_id, shop_id)
        async with Session() as s:
            return to_dto(await load(s, order_id))

    @app.post("/api/orders", tags=["boutiques"], status_code=201, summary="Pré-réserver une commande")
    async def create_order(
        body: OrderIn,
        response: Response,
        who: Identity = Depends(require_shop),
        idempotency_key: str | None = Header(None, max_length=80,
                                             description="Rejouer la même clé ne crée pas de doublon"),
    ):
        return await place_order(body, who, idempotency_key, response)

    async def finalize(order_id: str, shop_id: str) -> dict:
        now = utcnow_naive()
        async with Session.begin() as s:
            res = await s.execute(
                update(Order)
                .where(Order.id == order_id, Order.shop_id == shop_id,
                       Order.state == "pending", Order.expires_at > now)
                .values(state="confirmed", finalized_at=now)
                .execution_options(synchronize_session=False)
            )
            changed = res.rowcount == 1
        async with Session() as s:
            order = await load(s, order_id)
        if order is None or order.shop_id != shop_id:
            raise HTTPException(404, "Commande introuvable")
        if not changed:
            if order.state == "confirmed":
                return to_dto(order)  # déjà finalisée : idempotent
            if order.state == "pending":  # échéance dépassée, pas encore balayée
                await sweep_once()
            raise HTTPException(409, "La pré-réservation a expiré : le stock a été remis à disposition. "
                                     "Repassez la commande.")
        await bus.publish(Event.build(ORDER_CONFIRMED, SERVICE, to_payload(order)))
        log.info("Commande %s finalisée par %s", order_id, shop_id)
        return to_dto(order)

    @app.post("/api/orders/{order_id}/confirm", tags=["boutiques"], summary="Finaliser une commande pré-réservée")
    async def confirm_order(order_id: str, who: Identity = Depends(require_shop)):
        return await finalize(order_id, who.shop_id)

    @app.post("/api/orders/{order_id}/cancel", tags=["boutiques"], summary="Annuler une pré-réservation")
    async def cancel_order(order_id: str, who: Identity = Depends(require_shop)):
        async with Session.begin() as s:
            res = await s.execute(
                update(Order)
                .where(Order.id == order_id, Order.shop_id == who.shop_id, Order.state == "pending")
                .values(state="cancelled")
                .execution_options(synchronize_session=False)
            )
        async with Session() as s:
            order = await load(s, order_id)
        if order is None or order.shop_id != who.shop_id:
            raise HTTPException(404, "Commande introuvable")
        if res.rowcount != 1 and order.state != "cancelled":
            raise HTTPException(409, "Seule une commande pré-réservée peut être annulée")
        await release(order_id)
        async with Session() as s:
            return to_dto(await load(s, order_id))

    @app.get("/api/orders", tags=["boutiques"], summary="Commandes de la boutique")
    async def list_orders(who: Identity = Depends(require_shop)):
        async with Session() as s:
            orders = (
                await s.execute(select(Order).where(Order.shop_id == who.shop_id).order_by(Order.created_at.desc()))
            ).unique().scalars().all()
            return [to_dto(o) for o in orders]

    @app.get("/api/orders/{order_id}", tags=["boutiques"], summary="Détail d'une commande")
    async def get_order(order_id: str, who: Identity = Depends(require_shop)):
        async with Session() as s:
            order = await load(s, order_id)
        if order is None or order.shop_id != who.shop_id:
            raise HTTPException(404, "Commande introuvable")
        return to_dto(order)

    # ------------------------------------------------------------ Surfwear
    @app.get("/api/admin/orders", tags=["Surfwear"], summary="Toutes les commandes")
    async def admin_orders(_: Identity = Depends(require_staff), limit: int = Query(200, le=1000)):
        async with Session() as s:
            orders = (
                await s.execute(select(Order).order_by(Order.created_at.desc()).limit(limit))
            ).unique().scalars().all()
            return [to_dto(o) for o in orders]

    # ------------------------------------------------------------ interne
    @app.post("/internal/orders", tags=["interne"], status_code=201,
              summary="Pré-réserver (appel du service Panier, identité transmise)")
    async def internal_create(body: OrderIn, response: Response, who: Identity = Depends(require_shop),
                              idempotency_key: str | None = Header(None, max_length=80)):
        return await place_order(body, who, idempotency_key, response)

    @app.get("/internal/orders/{order_id}", tags=["interne"], summary="Commande (service Facturation)")
    async def internal_order(order_id: str):
        async with Session() as s:
            order = await load(s, order_id)
        if order is None:
            raise HTTPException(404, "Commande introuvable")
        return to_dto(order)

    return app
