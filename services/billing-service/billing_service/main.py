"""Service Facturation : une facture par expédition, téléchargeable en PDF.

À chaque ``shipment.dispatched``, la facture reprend les quantités réellement
expédiées, au prix de la commande et au taux de TVA du produit. Elle est numérotée
sans trou par année (FAC-2026-00001), publiée (``invoice.issued``) et mise à
disposition de la boutique.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import JSON, Date, DateTime, Float, Integer, String, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import INVOICE_ISSUED, SHIPMENT_DISPATCHED, Event, InvoicePayload, ShipmentDispatched, ShopRef
from surfwear_common.identity import Identity, require_identity, require_staff
from surfwear_common.service import env, setup_logging

from .pdf import render

SERVICE = "billing-service"
log = setup_logging(SERVICE)
PAYMENT_DAYS = 30


class Base(DeclarativeBase):
    pass


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    shipment_id: Mapped[str] = mapped_column(String(40), unique=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    shop: Mapped[dict] = mapped_column(JSON)
    lines: Mapped[list] = mapped_column(JSON)
    total_ht: Mapped[float] = mapped_column(Float)
    total_vat: Mapped[float] = mapped_column(Float)
    total_ttc: Mapped[float] = mapped_column(Float)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default="issued")  # issued | paid
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def dto(i: Invoice) -> dict:
    return {"id": i.id, "order_id": i.order_id, "shipment_id": i.shipment_id, "shop_id": i.shop_id,
            "shop": i.shop, "lines": i.lines, "total_ht": i.total_ht, "total_vat": i.total_vat,
            "total_ttc": i.total_ttc, "issued_at": i.issued_at.isoformat(), "due_date": i.due_date.isoformat(),
            "status": i.status, "paid_at": i.paid_at.isoformat() if i.paid_at else None,
            "overdue": i.status != "paid" and i.due_date < date.today()}


def create_app(database_url: str | None = None, bus: EventBus | None = None,
               orders_client: httpx.AsyncClient | None = None, identity_client: httpx.AsyncClient | None = None,
               catalog_client: httpx.AsyncClient | None = None) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./billing.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    orders = orders_client or httpx.AsyncClient(base_url=env("ORDERS_URL", "http://order-service:8000"), timeout=5)
    identity = identity_client or httpx.AsyncClient(base_url=env("IDENTITY_URL", "http://identity-service:8000"), timeout=5)
    catalog = catalog_client or httpx.AsyncClient(base_url=env("CATALOG_URL", "http://catalog-service:8000"), timeout=5)

    async def on_shipment(event: Event) -> None:
        sh = ShipmentDispatched(**event.data)
        async with Session() as s:
            if (await s.execute(select(Invoice).where(Invoice.shipment_id == sh.shipment_id))).scalar_one_or_none():
                return  # déjà facturé (message rejoué)
        order = (await orders.get(f"/internal/orders/{sh.order_id}")).raise_for_status().json()
        shop = (await identity.get(f"/internal/shops/{sh.shop.id}")).raise_for_status().json()
        products = {p["sku"]: p for p in (await catalog.get(
            "/internal/products", params=[("sku", l.sku) for l in sh.lines])).raise_for_status().json()}
        prices = {l["sku"]: l["unit_price"] for l in order["lines"]}
        lines, total_ht, total_vat = [], 0.0, 0.0
        for l in sh.lines:
            price = prices.get(l.sku, products.get(l.sku, {}).get("unit_price", 0.0))
            rate = products.get(l.sku, {}).get("vat_rate", 0.20)
            ht = round(price * l.qty, 2)
            total_ht += ht
            total_vat += ht * rate
            lines.append({"sku": l.sku, "name": l.name, "qty": l.qty, "unit_price": price,
                          "vat_rate": rate, "total_ht": ht})
        total_ht, total_vat = round(total_ht, 2), round(total_vat, 2)
        now = datetime.now(timezone.utc)
        for _ in range(5):  # numérotation sans trou : nouvel essai si deux factures prennent le même numéro
            try:
                async with Session.begin() as s:
                    seq = ((await s.execute(select(func.max(Invoice.seq)).where(Invoice.year == now.year))
                            ).scalar() or 0) + 1
                    inv = Invoice(id=f"FAC-{now.year}-{seq:05d}", year=now.year, seq=seq,
                                  shipment_id=sh.shipment_id, order_id=sh.order_id, shop_id=sh.shop.id,
                                  shop={"id": shop["id"], "name": shop["name"], "address": shop.get("address", ""),
                                        "city": shop.get("city", ""), "siret": shop.get("siret", ""),
                                        "email": shop.get("email", "")},
                                  lines=lines, total_ht=total_ht, total_vat=total_vat,
                                  total_ttc=round(total_ht + total_vat, 2), issued_at=now,
                                  due_date=(now + timedelta(days=PAYMENT_DAYS)).date())
                    s.add(inv)
                break
            except IntegrityError:
                continue
        else:
            raise RuntimeError("Numérotation de facture impossible")
        await bus.publish(Event.build(INVOICE_ISSUED, SERVICE, InvoicePayload(
            invoice_id=inv.id, order_id=inv.order_id, shipment_id=inv.shipment_id,
            shop=ShopRef(id=shop["id"], name=shop["name"], connector=shop.get("connector", "none"),
                         email=shop.get("email", "")),
            total_ht=inv.total_ht, total_vat=inv.total_vat, total_ttc=inv.total_ttc, due_date=inv.due_date)))
        log.info("Facture %s émise pour %s (%s TTC)", inv.id, shop["name"], inv.total_ttc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        bus.subscribe(f"{SERVICE}.shipments", [SHIPMENT_DISPATCHED], on_shipment)
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Facturation", version="3.0.0",
                  description="Factures émises à l'expédition, consultables et téléchargeables en PDF.",
                  lifespan=lifespan)

    async def find(invoice_id: str, who: Identity) -> Invoice:
        async with Session() as s:
            inv = await s.get(Invoice, invoice_id)
        if inv is None or (not who.is_staff and inv.shop_id != who.shop_id):
            raise HTTPException(404, "Facture introuvable")
        return inv

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/api/invoices", tags=["factures"], summary="Factures (les siennes pour une boutique, toutes pour Surfwear)")
    async def list_invoices(who: Identity = Depends(require_identity), shop_id: str | None = Query(None)):
        async with Session() as s:
            q = select(Invoice).order_by(Invoice.issued_at.desc())
            if not who.is_staff:
                q = q.where(Invoice.shop_id == who.shop_id)
            elif shop_id:
                q = q.where(Invoice.shop_id == shop_id)
            return [dto(i) for i in (await s.execute(q)).scalars().all()]

    @app.get("/api/invoices/{invoice_id}", tags=["factures"], summary="Détail d'une facture")
    async def get_invoice(invoice_id: str, who: Identity = Depends(require_identity)):
        return dto(await find(invoice_id, who))

    @app.get("/api/invoices/{invoice_id}/pdf", tags=["factures"], summary="Télécharger la facture en PDF",
             response_class=Response)
    async def invoice_pdf(invoice_id: str, who: Identity = Depends(require_identity)):
        inv = await find(invoice_id, who)
        return Response(render(dto(inv)), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{inv.id}.pdf"',
                                 "Cache-Control": "private, no-store"})

    @app.post("/api/invoices/{invoice_id}/paid", tags=["Surfwear"], summary="Enregistrer le règlement")
    async def mark_paid(invoice_id: str, _: Identity = Depends(require_staff)):
        async with Session.begin() as s:
            inv = await s.get(Invoice, invoice_id)
            if inv is None:
                raise HTTPException(404, "Facture introuvable")
            inv.status, inv.paid_at = "paid", datetime.now(timezone.utc)
        return dto(inv)

    return app
