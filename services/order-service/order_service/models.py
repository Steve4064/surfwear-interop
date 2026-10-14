from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Shop(Base):
    """Boutique cliente : identité, éditeur de son logiciel, clé d'API."""

    __tablename__ = "shops"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80), default="")
    connector: Mapped[str] = mapped_column(String(10))  # "rest" (éditeur A) ou "csv" (éditeur B)
    email: Mapped[str] = mapped_column(String(120))
    api_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "idempotency_key", name="uq_order_idempotency"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    shop: Mapped[Shop] = relationship(lazy="joined")
    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", lazy="selectin", cascade="all, delete-orphan", order_by="OrderLine.id"
    )


class OrderLine(Base):
    """Une ligne de commande. Invariant :
    qty_ordered = qty_reserved + somme(scheduled) + qty_unscheduled + qty_shipped
    """

    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(120))
    unit_price: Mapped[float] = mapped_column(Float)
    qty_ordered: Mapped[int] = mapped_column(Integer)
    qty_reserved: Mapped[int] = mapped_column(Integer, default=0)
    scheduled: Mapped[list] = mapped_column(JSON, default=list)  # [{qty, eta, lot_id}]
    qty_unscheduled: Mapped[int] = mapped_column(Integer, default=0)
    qty_shipped: Mapped[int] = mapped_column(Integer, default=0)

    order: Mapped[Order] = relationship(back_populates="lines")
