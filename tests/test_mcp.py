"""Serveur MCP, de bout en bout : client MCP → passerelle (règles de kong.yml) → serveur MCP → microservices."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client  # noqa: compatibilité mcp 1.x

from conftest import key


@asynccontextmanager
async def mcp_session(eco, headers: dict):
    gateway = eco.client()._transport  # même passerelle ASGI que les autres tests

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=gateway, base_url="http://localhost", headers=headers,
                                 timeout=timeout, auth=auth)

    async with streamablehttp_client("http://localhost/mcp", headers=headers, httpx_client_factory=factory) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


async def personal_token(eco, email: str) -> dict:
    c = await eco.login(email)
    r = await c.post("/api/auth/token")
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def call(session: ClientSession, tool: str, **args):
    res = await session.call_tool(tool, args)
    text = res.content[0].text if res.content else ""
    if res.isError:
        return {"error": text}
    return res.structuredContent.get("result", res.structuredContent) if res.structuredContent else json.loads(text)


@pytest.mark.asyncio
async def test_gateway_requires_credentials(eco):
    r = await eco.client().post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={"Accept": "application/json, text/event-stream",
                                         "X-Auth-User": "pirate", "X-Auth-Role": "staff"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_shop_orders_through_mcp(eco):
    async with mcp_session(eco, await personal_token(eco, "biarritz@boutiques.example")) as s:
        tools = {t.name for t in (await s.list_tools()).tools}
        assert {"rechercher_produits", "commander_panier", "finaliser_commande", "tableau_de_bord"} <= tools

        me = await call(s, "qui_suis_je")
        assert me["role"] == "shop" and me["shop"]

        products = await call(s, "rechercher_produits")
        sku = next(p["sku"] for p in products if (p["available_now"] or 0) >= 2)
        assert (await call(s, "verifier_disponibilite", sku=sku, quantite=2))

        cart = await call(s, "modifier_panier", sku=sku, quantite=2)
        assert any(l["sku"] == sku and l["qty"] == 2 for l in cart["lines"])
        order = await call(s, "commander_panier")
        assert order["state"] == "pending" and order["expires_at"]

        done = await call(s, "finaliser_commande", commande_id=order["order_id"])
        assert done["state"] == "confirmed"
        mine = await call(s, "mes_commandes", etat="confirmed")
        assert [o["order_id"] for o in mine] == [order["order_id"]]

        # les outils de l'équipe Surfwear sont refusés à une boutique
        denied = await call(s, "tableau_de_bord")
        assert "réservé à l'équipe Surfwear" in denied["error"]


@pytest.mark.asyncio
async def test_shop_api_key_and_isolation(eco):
    async with mcp_session(eco, key("hossegor")) as s:
        me = await call(s, "qui_suis_je")
        assert me["shop"] == "SHOP-HOS"
        err = await call(s, "detail_commande", commande_id="CMD-INCONNUE")
        assert "error" in err


@pytest.mark.asyncio
async def test_staff_dashboard_and_warehouse(eco):
    async with mcp_session(eco, await personal_token(eco, "equipe@surfwear.example")) as s:
        board = await call(s, "tableau_de_bord", seuil_stock_bas=1000)
        assert board["stock_bas"] and "factures_impayees" in board

        stock = await call(s, "stock_entrepot")
        sku = stock["products"][0]["sku"]
        before = stock["products"][0]["on_hand"]
        await call(s, "enregistrer_reception", sku=sku, quantite=5, bon_livraison="BL-TEST")
        after = next(p for p in (await call(s, "stock_entrepot", recherche=sku))["products"] if p["sku"] == sku)
        assert after["on_hand"] == before + 5

        shops = await call(s, "boutiques", statut="active")
        assert len(shops) >= 4
        assert "réservé aux boutiques" in (await call(s, "voir_panier"))["error"]
