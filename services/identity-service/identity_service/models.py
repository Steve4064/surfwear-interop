from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Shop(Base):
    """Boutique cliente (compte B2B).

    status : pending (inscrite, en attente de validation par Surfwear),
             active, rejected, suspended
    connector : logiciel de la boutique, choisi par Surfwear à la validation
                ("rest" = éditeur A, "csv" = éditeur B, "none" = portail seul)
    """

    __tablename__ = "shops"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80), default="")
    address: Mapped[str] = mapped_column(String(200), default="")
    siret: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(120))
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    connector: Mapped[str] = mapped_column(String(10), default="none")
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    api_key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    zitadel_org_id: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(120), default="")


class LocalUser(Base):
    """Comptes du mode sans ZITADEL (AUTH_PROVIDER=local : tests et démonstration hors ligne)."""

    __tablename__ = "local_users"

    username: Mapped[str] = mapped_column(String(120), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10))
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
