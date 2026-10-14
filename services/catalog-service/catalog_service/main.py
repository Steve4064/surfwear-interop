"""Service Catalogue : fiches produits (référentiel) et catalogue affiché aux boutiques.

* Les boutiques consultent le catalogue actif, enrichi de la disponibilité calculée
  par le Service Stock.
* L'équipe Surfwear crée et modifie les produits ; chaque création ou modification
  est publiée (``product.created`` / ``product.updated``) pour que le Stock, l'Entrepôt
  et la Facturation se mettent à jour.
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, DateTime, Float, String, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.db import create_schema, make_engine, make_sessionmaker
from surfwear_common.events import PRODUCT_CREATED, PRODUCT_UPDATED, Event, ProductPayload
from surfwear_common.identity import Identity, require_identity, require_staff
from surfwear_common.service import env, setup_logging

SERVICE = "catalog-service"
log = setup_logging(SERVICE)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    __tablename__ = "products"
    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text, default="")
    unit_price: Mapped[float] = mapped_column(Float)
    vat_rate: Mapped[float] = mapped_column(Float, default=0.20)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProductIn(BaseModel):
    sku: str = Field(description="Référence, ex. SW-PAR-009")
    name: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=2, max_length=60)
    description: str = Field("", max_length=2000)
    unit_price: float = Field(gt=0, le=100_000, description="Prix de vente HT aux boutiques")
    vat_rate: float = Field(0.20, ge=0, le=0.5)
    active: bool = True

    @field_validator("sku")
    @classmethod
    def sku_format(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,31}", v):
            raise ValueError("Référence : 3 à 32 caractères, lettres, chiffres et tirets")
        return v


class ProductPatch(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=120)
    category: str | None = Field(None, min_length=2, max_length=60)
    description: str | None = Field(None, max_length=2000)
    unit_price: float | None = Field(None, gt=0, le=100_000)
    vat_rate: float | None = Field(None, ge=0, le=0.5)
    active: bool | None = None


def dto(p: Product) -> dict:
    return {"sku": p.sku, "name": p.name, "category": p.category, "description": p.description,
            "unit_price": p.unit_price, "vat_rate": p.vat_rate, "active": p.active}


def create_app(database_url: str | None = None, bus: EventBus | None = None,
               stock_client: httpx.AsyncClient | None = None, catalog_file: str | None = None) -> FastAPI:
    engine = make_engine(database_url or env("DATABASE_URL", "sqlite+aiosqlite:///./catalog.db"))
    Session = make_sessionmaker(engine)
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    stock = stock_client or httpx.AsyncClient(base_url=env("STOCK_URL", "http://stock-service:8000"), timeout=5)
    seed_path = Path(catalog_file or env("CATALOG_FILE", "/config/catalog.json"))

    async def seed() -> None:
        async with Session.begin() as s:
            if (await s.execute(select(func.count()).select_from(Product))).scalar_one() or not seed_path.exists():
                return
            for p in json.loads(seed_path.read_text(encoding="utf-8"))["products"]:
                s.add(Product(sku=p["sku"], name=p["name"], category=p["category"],
                              description=p.get("description", ""), unit_price=p["unit_price"],
                              vat_rate=p.get("vat_rate", 0.20)))
        log.info("Catalogue initialisé depuis %s", seed_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await create_schema(engine, Base)
        await seed()
        await bus.connect()
        yield
        await bus.close()
        await engine.dispose()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Catalogue", version="3.0.0",
                  description="Référentiel produits et catalogue des boutiques.", lifespan=lifespan)

    async def availability() -> dict[str, dict]:
        try:
            r = await stock.get("/internal/stock")
            r.raise_for_status()
            return {x["sku"]: x for x in r.json()}
        except httpx.HTTPError as exc:
            log.warning("Disponibilité indisponible : %s", exc)
            return {}

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/api/catalog", tags=["boutiques"], summary="Catalogue avec disponibilité")
    async def catalog(_: Identity = Depends(require_identity), q: str | None = None,
                      category: str | None = None):
        async with Session() as s:
            query = select(Product).where(Product.active.is_(True)).order_by(Product.category, Product.name)
            rows = (await s.execute(query)).scalars().all()
        avail = await availability()
        out = []
        for p in rows:
            if category and p.category != category:
                continue
            if q and q.lower() not in f"{p.name} {p.sku} {p.description}".lower():
                continue
            a = avail.get(p.sku, {})
            out.append({**dto(p), "available_now": a.get("available_now"), "upcoming": a.get("upcoming", []),
                        "stock_known": bool(a)})
        return out

    @app.get("/api/catalog/{sku}", tags=["boutiques"], summary="Fiche produit")
    async def product_sheet(sku: str, _: Identity = Depends(require_identity)):
        async with Session() as s:
            p = await s.get(Product, sku.upper())
        if p is None or not p.active:
            raise HTTPException(404, "Produit introuvable")
        a = (await availability()).get(p.sku, {})
        return {**dto(p), "available_now": a.get("available_now"), "upcoming": a.get("upcoming", [])}

    # ------------------------------------------------------------ Surfwear
    @app.get("/api/products", tags=["Surfwear"], summary="Tous les produits, y compris inactifs")
    async def all_products(_: Identity = Depends(require_staff)):
        async with Session() as s:
            rows = (await s.execute(select(Product).order_by(Product.sku))).scalars().all()
        avail = await availability()
        return [{**dto(p), "available_now": avail.get(p.sku, {}).get("available_now"),
                 "on_hand": avail.get(p.sku, {}).get("on_hand"), "reserved": avail.get(p.sku, {}).get("reserved")}
                for p in rows]

    @app.post("/api/products", tags=["Surfwear"], status_code=201, summary="Ajouter un produit")
    async def create_product(body: ProductIn, who: Identity = Depends(require_staff)):
        async with Session.begin() as s:
            if await s.get(Product, body.sku):
                raise HTTPException(409, f"La référence {body.sku} existe déjà")
            p = Product(**body.model_dump())
            s.add(p)
        await bus.publish(Event.build(PRODUCT_CREATED, SERVICE, ProductPayload(**dto(p))))
        log.info("Produit %s créé par %s", p.sku, who.name)
        return dto(p)

    @app.patch("/api/products/{sku}", tags=["Surfwear"], summary="Modifier un produit")
    async def update_product(sku: str, body: ProductPatch, _: Identity = Depends(require_staff)):
        async with Session.begin() as s:
            p = await s.get(Product, sku.upper())
            if p is None:
                raise HTTPException(404, "Produit introuvable")
            for k, v in body.model_dump(exclude_none=True).items():
                setattr(p, k, v)
        await bus.publish(Event.build(PRODUCT_UPDATED, SERVICE, ProductPayload(**dto(p))))
        return dto(p)

    # ------------------------------------------------------------ interne
    @app.get("/internal/products", tags=["interne"])
    async def internal_products(sku: list[str] = Query(default_factory=list)):
        async with Session() as s:
            q = select(Product)
            if sku:
                q = q.where(Product.sku.in_([x.upper() for x in sku]))
            return [dto(p) for p in (await s.execute(q)).scalars().all()]

    return app
