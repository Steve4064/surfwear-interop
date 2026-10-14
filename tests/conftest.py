"""Écosystème complet en mémoire : tous les services, un bus en mémoire, SQLite.

Les services communiquent exactement comme en production (REST entre services,
événements sur le bus), sans Docker ni RabbitMQ.
"""
from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "common"))
for service in (ROOT / "services").iterdir():
    sys.path.insert(0, str(service))

from erp_connector.main import create_app as create_erp  # noqa: E402
from notification_service.main import create_app as create_notifications  # noqa: E402
from order_service.main import create_app as create_orders  # noqa: E402
from shop_connector.adapters import VendorARestAdapter, VendorBCsvAdapter  # noqa: E402
from shop_connector.main import create_app as create_connector  # noqa: E402
from shop_simulator.main import create_app as create_simulator  # noqa: E402
from stock_service.main import create_app as create_stock  # noqa: E402
from surfwear_common.bus import MemoryBus  # noqa: E402

SECRET = "test-secret"


def client(app, name: str, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{name}", **kw)


@pytest_asyncio.fixture
async def eco(tmp_path):
    bus = MemoryBus()
    exchange = tmp_path / "exchange"
    db = lambda name: f"sqlite+aiosqlite:///{tmp_path / name}.db"  # noqa: E731

    erp = create_erp(db("erp"), bus, catalog_file=str(ROOT / "config" / "catalog.json"))
    erp_c = client(erp, "erp")
    stock = create_stock(db("stock"), bus, erp_c)
    stock_c = client(stock, "stock")
    orders = create_orders(db("orders"), bus, stock_c, shops_file=str(ROOT / "config" / "shops.json"))
    simulator = create_simulator(secret=SECRET, exchange_dir=str(exchange))
    sim_c = client(simulator, "sim")
    conn_rest = create_connector(VendorARestAdapter(sim_c, SECRET), bus)
    conn_csv = create_connector(VendorBCsvAdapter(str(exchange)), bus)
    notif = create_notifications(bus, smtp_host="")

    async with AsyncExitStack() as stack:
        for app in (erp, stock, orders, conn_rest, conn_csv, notif):
            await stack.enter_async_context(app.router.lifespan_context(app))
        clients = [erp_c, stock_c, sim_c, client(orders, "orders"), client(notif, "notif"),
                   client(conn_rest, "rest"), client(conn_csv, "csv")]
        for c in clients:
            stack.push_async_callback(c.aclose)
        yield SimpleNamespace(
            bus=bus, erp=erp_c, stock=stock_c, sim=sim_c, orders=clients[3], notif=clients[4],
            conn_rest=clients[5], conn_csv=clients[6], simulator=simulator, exchange=exchange,
        )


def key(shop: str) -> dict:
    return {"X-API-Key": f"demo-key-{shop}"}
