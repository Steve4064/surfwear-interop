"""Identité transmise par la passerelle.

La passerelle authentifie chaque requête auprès du service Identité (auth_request)
puis transmet l'identité vérifiée aux services dans des en-têtes internes. Elle
efface systématiquement ces en-têtes s'ils viennent du client : un service ne
les reçoit donc que de la passerelle. Les services ne sont pas exposés
directement (réseau Docker interne).

    X-Auth-User   identifiant de l'utilisateur (ou "api:<boutique>")
    X-Auth-Role   "shop" ou "staff"
    X-Auth-Shop   identifiant de la boutique (rôle shop)
    X-Auth-Name   nom affiché
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class Identity:
    user: str
    role: str
    shop_id: str | None
    name: str

    @property
    def is_staff(self) -> bool:
        return self.role == "staff"


def auth_headers(identity: Identity) -> dict[str, str]:
    """En-têtes à transmettre lors d'un appel interne entre services."""
    h = {"X-Auth-User": identity.user, "X-Auth-Role": identity.role, "X-Auth-Name": quote(identity.name)}
    if identity.shop_id:
        h["X-Auth-Shop"] = identity.shop_id
    return h


async def optional_identity(
    x_auth_user: str | None = Header(None, include_in_schema=False),
    x_auth_role: str | None = Header(None, include_in_schema=False),
    x_auth_shop: str | None = Header(None, include_in_schema=False),
    x_auth_name: str | None = Header(None, include_in_schema=False),
) -> Identity | None:
    if not x_auth_user or x_auth_role not in ("shop", "staff"):
        return None
    if x_auth_role == "shop" and not x_auth_shop:
        return None
    return Identity(x_auth_user, x_auth_role, x_auth_shop or None, unquote(x_auth_name or ""))


async def require_identity(x_auth_user: str | None = Header(None, include_in_schema=False),
                           x_auth_role: str | None = Header(None, include_in_schema=False),
                           x_auth_shop: str | None = Header(None, include_in_schema=False),
                           x_auth_name: str | None = Header(None, include_in_schema=False)) -> Identity:
    ident = await optional_identity(x_auth_user, x_auth_role, x_auth_shop, x_auth_name)
    if ident is None:
        raise HTTPException(401, "Connexion requise")
    return ident


async def require_shop(x_auth_user: str | None = Header(None, include_in_schema=False),
                       x_auth_role: str | None = Header(None, include_in_schema=False),
                       x_auth_shop: str | None = Header(None, include_in_schema=False),
                       x_auth_name: str | None = Header(None, include_in_schema=False)) -> Identity:
    ident = await require_identity(x_auth_user, x_auth_role, x_auth_shop, x_auth_name)
    if ident.role != "shop":
        raise HTTPException(403, "Réservé aux boutiques")
    return ident


async def require_staff(x_auth_user: str | None = Header(None, include_in_schema=False),
                        x_auth_role: str | None = Header(None, include_in_schema=False),
                        x_auth_shop: str | None = Header(None, include_in_schema=False),
                        x_auth_name: str | None = Header(None, include_in_schema=False)) -> Identity:
    ident = await require_identity(x_auth_user, x_auth_role, x_auth_shop, x_auth_name)
    if ident.role != "staff":
        raise HTTPException(403, "Réservé à l'équipe Surfwear")
    return ident
