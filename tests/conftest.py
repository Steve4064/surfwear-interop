"""Écosystème complet en mémoire, derrière la passerelle locale.

Tous les microservices tournent dans le processus de test, avec un bus en mémoire
et SQLite. Les requêtes passent par la passerelle locale (mêmes règles d'accès que
Kong, lues dans gateway/kong.yml) : le contrôle d'accès est donc testé de bout en bout. Le service Identité
utilise les comptes locaux (AUTH_PROVIDER=local) ; l'intégration ZITADEL est testée
à part avec un faux fournisseur OIDC (test_zitadel.py).
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from local_gateway import build_gateway  # noqa: E402
from local_stack import CONFIG, PASSWORDS, asgi, build  # noqa: E402
from surfwear_common.bus import MemoryBus  # noqa: E402


@pytest_asyncio.fixture
async def eco(tmp_path):
    bus = MemoryBus()
    apps, exchange = build(tmp_path, bus)
    gateway = build_gateway(apps, CONFIG.parent / "portal")
    clients: list[httpx.AsyncClient] = []

    def client(**kw) -> httpx.AsyncClient:
        c = asgi(gateway, "localhost", **kw)
        clients.append(c)
        return c

    async def login(email: str) -> httpx.AsyncClient:
        c = client()
        r = await c.post("/api/auth/login", json={"username": email, "password": PASSWORDS[email]})
        assert r.status_code == 200, r.text
        return c

    async with AsyncExitStack() as stack:
        for name in ("warehouse", "stock", "identity", "catalog", "orders", "cart", "billing",
                     "conn_rest", "conn_csv", "notifications"):
            app = apps[name]
            await stack.enter_async_context(app.router.lifespan_context(app))
        await _serve_mcp(stack, apps["mcp"])
        stack.push_async_callback(lambda: _close(clients))
        yield SimpleNamespace(bus=bus, apps=apps, exchange=exchange, client=client, login=login,
                              sweep=apps["orders"].state.sweep_once, orders_session=apps["orders"].state.session)


async def _serve_mcp(stack: AsyncExitStack, app) -> None:
    """Le gestionnaire de sessions MCP (groupe de tâches anyio) doit s'ouvrir et se fermer
    dans la même tâche : on le fait vivre dans une tâche dédiée pendant le test."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def run():
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run())
    await started.wait()

    async def shutdown():
        stop.set()
        await task
    stack.push_async_callback(shutdown)


async def _close(clients):
    for c in clients:
        await c.aclose()


def key(shop: str) -> dict:
    return {"X-API-Key": f"demo-key-{shop}"}
