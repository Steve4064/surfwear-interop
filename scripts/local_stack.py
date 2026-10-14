"""Assemblage de tout l'écosystème dans un seul processus (bus en mémoire, SQLite).

Utilisé par les tests et par le mode démonstration sans Docker (dev_server.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "common"))
for service in (ROOT / "services").iterdir():
    sys.path.insert(0, str(service))

from billing_service.main import create_app as create_billing
from cart_service.main import create_app as create_cart
from catalog_service.main import create_app as create_catalog
from identity_service.main import create_app as create_identity
from notification_service.main import create_app as create_notifications
from order_service.main import create_app as create_orders
from shop_connector.adapters import VendorARestAdapter, VendorBCsvAdapter
from shop_connector.main import create_app as create_connector
from shop_simulator.main import create_app as create_simulator
from stock_service.main import create_app as create_stock
from surfwear_common.bus import MemoryBus
from warehouse_service.main import create_app as create_warehouse

SECRET = "test-secret"
CONFIG = ROOT / "config"
PASSWORDS = {
    "equipe@surfwear.example": "Surfwear2026!",
    "biarritz@boutiques.example": "Biarritz2026!",
    "hossegor@boutiques.example": "Hossegor2026!",
    "lacanau@boutiques.example": "Lacanau2026!",
    "arcachon@boutiques.example": "Arcachon2026!",
}


def asgi(app, name: str, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{name}", **kw)


def build(tmp_path: Path, bus: MemoryBus, hold_minutes: float = 15, sweep_seconds: float = 0):
    exchange = tmp_path / "exchange"
    db = lambda name: f"sqlite+aiosqlite:///{tmp_path / name}.db"  # noqa: E731
    a: dict = {}
    a["warehouse"] = create_warehouse(db("warehouse"), bus, catalog_file=str(CONFIG / "catalog.json"))
    a["stock"] = create_stock(db("stock"), bus, asgi(a["warehouse"], "warehouse"))
    a["identity"] = create_identity(db("identity"), bus, provider="local", auth_secret="test",
                                    shops_file=str(CONFIG / "shops.json"), users_file=str(CONFIG / "users.json"))
    a["catalog"] = create_catalog(db("catalog"), bus, asgi(a["stock"], "stock"),
                                  catalog_file=str(CONFIG / "catalog.json"))
    a["orders"] = create_orders(db("orders"), bus, asgi(a["stock"], "stock"),
                                identity_client=asgi(a["identity"], "identity"),
                                hold_minutes=hold_minutes, sweep_seconds=sweep_seconds)
    a["cart"] = create_cart(db("cart"), asgi(a["catalog"], "catalog"), asgi(a["orders"], "orders"))
    a["billing"] = create_billing(db("billing"), bus, asgi(a["orders"], "orders"), asgi(a["identity"], "identity"),
                                  asgi(a["catalog"], "catalog"))
    a["simulator"] = create_simulator(secret=SECRET, exchange_dir=str(exchange))
    a["conn_rest"] = create_connector(VendorARestAdapter(asgi(a["simulator"], "sim"), SECRET), bus)
    a["conn_csv"] = create_connector(VendorBCsvAdapter(str(exchange)), bus)
    a["notifications"] = create_notifications(bus, smtp_host="")
    return a, exchange


