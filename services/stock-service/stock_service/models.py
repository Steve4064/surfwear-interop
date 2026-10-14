from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, CheckConstraint, Date, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Product(Base):
    """Référence produit, miroir du stock physique de l'ERP + quantités réservées."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("reserved >= 0", name="reserved_positive"),
        CheckConstraint("reserved <= on_hand", name="reserved_le_on_hand"),
    )

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60), default="")
    unit_price: Mapped[float] = mapped_column(Float)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)  # stock réel en entrepôt
    reserved: Mapped[int] = mapped_column(Integer, default=0)  # déjà promis à des boutiques


class Lot(Base):
    """Lot en production (stock potentiel), daté."""

    __tablename__ = "lots"
    __table_args__ = (CheckConstraint("allocated <= qty", name="allocated_le_qty"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    allocated: Mapped[int] = mapped_column(Integer, default=0)
    eta: Mapped[date] = mapped_column(Date)
    received: Mapped[bool] = mapped_column(Boolean, default=False)


class Allocation(Base):
    """Part d'une ligne de commande affectée à une source.

    kind = "stock"       : réservée sur le stock réel (expédiable)
           "lot"         : planifiée sur un lot en production (date prévue)
           "unscheduled" : en attente, sans lot identifié
    L'identifiant croissant sert d'ordre d'arrivée (FIFO) pour servir les reliquats.
    """

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    sku: Mapped[str] = mapped_column(String(32), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    lot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    eta: Mapped[date | None] = mapped_column(Date, nullable=True)
