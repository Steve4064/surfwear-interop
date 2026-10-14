from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_naive() -> datetime:
    """Heure UTC sans fuseau, pour les comparaisons d'échéance (SQLite et PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Order(Base):
    """Cycle de vie (state) :
    pending   : pré-réservée, le stock est bloqué jusqu'à expires_at
    confirmed : finalisée par la boutique, transmise à l'entrepôt
    cancelled : annulée avant finalisation, stock libéré
    expired   : non finalisée à temps, stock libéré

    Les coordonnées de la boutique sont figées à la commande (nom, connecteur, e-mail).
    """

    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "idempotency_key", name="uq_order_idempotency"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    shop_name: Mapped[str] = mapped_column(String(120))
    shop_connector: Mapped[str] = mapped_column(String(10), default="none")
    shop_email: Mapped[str] = mapped_column(String(120), default="")
    placed_by: Mapped[str] = mapped_column(String(120), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    lines: Mapped[list["OrderLine"]] = relationship(
        back_populates="order", lazy="selectin", cascade="all, delete-orphan", order_by="OrderLine.id"
    )


class OrderLine(Base):
    """qty_ordered = qty_reserved + somme(scheduled) + qty_unscheduled + qty_shipped"""

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
