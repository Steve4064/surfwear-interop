"""Connecteur boutiques : une image, un adaptateur par éditeur (variable ADAPTER)."""
from __future__ import annotations

from collections import deque
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.events import ORDER_CONFIRMED, ORDER_UPDATED, SHIPMENT_DISPATCHED, Event
from surfwear_common.service import env, setup_logging

from .adapters import Adapter, VendorARestAdapter, VendorBCsvAdapter

SERVICE = "shop-connector"
log = setup_logging(SERVICE)


def make_adapter(kind: str, http_client: httpx.AsyncClient | None = None) -> Adapter:
    if kind == "rest":
        client = http_client or httpx.AsyncClient(base_url=env("VENDOR_A_URL", "http://shop-simulator:8000"), timeout=5)
        return VendorARestAdapter(client, env("WEBHOOK_SECRET", "change-me"))
    if kind == "csv":
        return VendorBCsvAdapter(env("EXCHANGE_DIR", "/exchange"))
    raise ValueError(f"Adaptateur inconnu : {kind}")


def create_app(adapter: Adapter | None = None, bus: EventBus | None = None) -> FastAPI:
    adapter = adapter or make_adapter(env("ADAPTER", "rest"))
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    journal: deque = deque(maxlen=100)
    stats = {"delivered": 0, "ignored": 0}

    async def on_event(event: Event) -> None:
        if event.data.get("shop", {}).get("connector") != adapter.name:
            stats["ignored"] += 1  # boutique d'un autre éditeur
            return
        result = await adapter.deliver(event)
        stats["delivered"] += 1
        journal.appendleft({"event": event.type, "order_id": event.data.get("order_id"), "result": result})
        log.info("%s %s -> %s", event.type, event.data.get("order_id"), result)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.subscribe(f"{SERVICE}.{adapter.name}", [ORDER_CONFIRMED, ORDER_UPDATED, SHIPMENT_DISPATCHED], on_event)
        await bus.connect()
        yield
        await bus.close()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title=f"Connecteur boutiques ({adapter.name})", version="1.0.0", lifespan=lifespan)

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE, "adapter": adapter.name}

    @app.get("/stats", tags=["supervision"], summary="Messages transmis aux boutiques")
    async def get_stats():
        return {"adapter": adapter.name, **stats, "journal": list(journal)}

    return app
