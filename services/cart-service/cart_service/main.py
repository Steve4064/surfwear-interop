"""Service Panier : panier de chaque utilisateur, conservé côté serveur.

Le panier suit l'utilisateur d'un appareil à l'autre. Les prix et la disponibilité
sont relus à chaque affichage (Catalogue), jamais figés dans le panier. La
validation du panier crée une pré-réservation auprès du Service Commandes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Integer, String, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.identity import Identity, auth_headers, require_shop
from surfwear_common.service import env, setup_logging

SERVICE = "cart-service"
log = setup_logging(SERVICE)


class Base(DeclarativeBase):
    pass


class CartItem(Base):
    __tablename__ = "cart_items"
    user: Mapped[str] = mapped_column(String(120), primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                               default=lambda: datetime.now(timezone.utc))


class QtyIn(BaseModel):
    qty: int = Field(ge=0, le=10_000, description="0 retire l'article du panier")


def plan(available: int | None, upcoming: list[dict], qty: int) -> dict:
    """Ce qui partirait tout de suite, plus tard, ou sans date, si l'on pré-réservait maintenant."""
    now = min(qty, max(available or 0, 0))
    rest, later = qty - now, []
    for u in upcoming:
        if not rest:
            break
        t = min(rest, u["available"])
        if t:
            later.append({"qty": t, "eta": u["eta"]})
            rest -= t
    return {"now": now, "later": later, "unscheduled": rest}


def create_app(database_url: str | None = None, catalog_client: httpx.AsyncClient | None = None,
               orders_client: httpx.AsyncClient | None = None) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./cart.db"))
    Session = make_sessionmaker(engine)
    catalog = catalog_client or httpx.AsyncClient(base_url=env("CATALOG_URL", "http://catalog-service:8000"), timeout=5)
    orders = orders_client or httpx.AsyncClient(base_url=env("ORDERS_URL", "http://order-service:8000"), timeout=10)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        yield
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Panier", version="3.0.0",
                  description="Panier des boutiques et passage à la pré-réservation.", lifespan=lifespan)

    async def items(user: str) -> list[CartItem]:
        async with Session() as s:
            return list((await s.execute(select(CartItem).where(CartItem.user == user)
                                         .order_by(CartItem.added_at))).scalars())

    async def view(who: Identity) -> dict:
        rows = await items(who.user)
        products: dict[str, dict] = {}
        if rows:
            try:
                r = await catalog.get("/api/catalog", headers=auth_headers(who))
                r.raise_for_status()
                products = {p["sku"]: p for p in r.json()}
            except httpx.HTTPError as exc:
                log.warning("Catalogue indisponible : %s", exc)
                raise HTTPException(503, "Catalogue momentanément indisponible")
        lines, total_ht, total_vat = [], 0.0, 0.0
        for it in rows:
            p = products.get(it.sku)
            if p is None:  # produit retiré du catalogue depuis l'ajout
                lines.append({"sku": it.sku, "qty": it.qty, "available": False})
                continue
            line_ht = round(p["unit_price"] * it.qty, 2)
            total_ht += line_ht
            total_vat += line_ht * p["vat_rate"]
            lines.append({"sku": it.sku, "name": p["name"], "category": p["category"], "qty": it.qty,
                          "unit_price": p["unit_price"], "vat_rate": p["vat_rate"], "total_ht": line_ht,
                          "available": True, "plan": plan(p.get("available_now"), p.get("upcoming", []), it.qty)})
        total_ht, total_vat = round(total_ht, 2), round(total_vat, 2)
        return {"lines": lines, "count": sum(l["qty"] for l in lines), "total_ht": total_ht,
                "total_vat": total_vat, "total_ttc": round(total_ht + total_vat, 2)}

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/api/cart", tags=["panier"], summary="Contenu du panier, prix et disponibilité à jour")
    async def get_cart(who: Identity = Depends(require_shop)):
        return await view(who)

    @app.put("/api/cart/items/{sku}", tags=["panier"], summary="Fixer la quantité d'un article")
    async def set_qty(sku: str, body: QtyIn, who: Identity = Depends(require_shop)):
        sku = sku.upper()
        async with Session.begin() as s:
            item = await s.get(CartItem, (who.user, sku))
            if body.qty == 0:
                if item:
                    await s.delete(item)
            elif item:
                item.qty = body.qty
            else:
                s.add(CartItem(user=who.user, sku=sku, shop_id=who.shop_id, qty=body.qty))
        return await view(who)

    @app.delete("/api/cart/items/{sku}", tags=["panier"], summary="Retirer un article")
    async def remove(sku: str, who: Identity = Depends(require_shop)):
        async with Session.begin() as s:
            await s.execute(delete(CartItem).where(CartItem.user == who.user, CartItem.sku == sku.upper()))
        return await view(who)

    @app.delete("/api/cart", tags=["panier"], summary="Vider le panier")
    async def clear(who: Identity = Depends(require_shop)):
        async with Session.begin() as s:
            await s.execute(delete(CartItem).where(CartItem.user == who.user))
        return await view(who)

    @app.post("/api/cart/checkout", tags=["panier"], status_code=201,
              summary="Pré-réserver le contenu du panier")
    async def checkout(who: Identity = Depends(require_shop),
                       idempotency_key: str | None = Header(None, max_length=80)):
        rows = await items(who.user)
        if not rows:
            raise HTTPException(409, "Le panier est vide")
        headers = auth_headers(who)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = await orders.post("/internal/orders", headers=headers,
                                  json={"lines": [{"sku": i.sku, "qty": i.qty} for i in rows]})
        except httpx.HTTPError as exc:
            log.error("Service Commandes injoignable : %s", exc)
            raise HTTPException(503, "Service de commandes momentanément indisponible")
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.json().get("detail", "Commande refusée"))
        async with Session.begin() as s:
            await s.execute(delete(CartItem).where(CartItem.user == who.user))
        log.info("Panier de %s transformé en pré-réservation %s", who.user, r.json()["order_id"])
        return r.json()

    return app
