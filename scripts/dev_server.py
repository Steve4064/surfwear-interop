"""Mode démonstration sans Docker.

Lance tout l'écosystème dans un seul processus Python : mêmes services, mêmes
appels REST entre services, mêmes événements, mais avec un bus en mémoire et
des bases SQLite. Utile si Docker n'est pas disponible.

    pip install -r scripts/requirements-dev.txt
    python scripts/dev_server.py          # puis http://localhost:8080

Les données sont stockées dans ./.devdata (supprimer le dossier pour repartir à zéro).
"""
from __future__ import annotations

import mimetypes
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libs" / "common"))
for service in (ROOT / "services").iterdir():
    sys.path.insert(0, str(service))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from erp_connector.main import create_app as create_erp  # noqa: E402
from notification_service.main import create_app as create_notifications  # noqa: E402
from order_service.main import create_app as create_orders  # noqa: E402
from shop_connector.adapters import VendorARestAdapter, VendorBCsvAdapter  # noqa: E402
from shop_connector.main import create_app as create_connector  # noqa: E402
from shop_simulator.main import create_app as create_simulator  # noqa: E402
from stock_service.main import create_app as create_stock  # noqa: E402
from surfwear_common.bus import MemoryBus  # noqa: E402

DATA = ROOT / ".devdata"
DATA.mkdir(exist_ok=True)
SECRET = "dev-secret"
PORTAL = ROOT / "portal"


def db(name: str) -> str:
    return f"sqlite+aiosqlite:///{DATA / name}.db"


def internal(app, name: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{name}", timeout=10)


bus = MemoryBus()
erp = create_erp(db("erp"), bus, catalog_file=str(ROOT / "config" / "catalog.json"))
stock = create_stock(db("stock"), bus, internal(erp, "erp"))
orders = create_orders(db("orders"), bus, internal(stock, "stock"), shops_file=str(ROOT / "config" / "shops.json"),
                       users_file=str(ROOT / "config" / "users.json"))
orders_client = internal(orders, "orders")
simulator = create_simulator(secret=SECRET, exchange_dir=str(DATA / "exchange"))
conn_rest = create_connector(VendorARestAdapter(internal(simulator, "sim"), SECRET), bus)
conn_csv = create_connector(VendorBCsvAdapter(str(DATA / "exchange")), bus)
notifications = create_notifications(bus, smtp_host="")

PREFIXED = {  # équivalent des routes /svc/... de la passerelle Nginx
    "/svc/stock": stock, "/svc/orders": orders, "/svc/erp": erp,
    "/svc/notifications": notifications, "/svc/connector-rest": conn_rest,
    "/svc/connector-csv": conn_csv, "/boutiques": simulator,
}


async def static(scope, receive, send):
    path = scope["path"].lstrip("/") or "index.html"
    file = (PORTAL / path).resolve()
    if not file.is_file() or PORTAL not in file.parents:
        file = PORTAL / "index.html"
    body = file.read_bytes()
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan():
    async with AsyncExitStack() as stack:
        for app in (erp, stock, orders, conn_rest, conn_csv, notifications):
            await stack.enter_async_context(app.router.lifespan_context(app))
        yield


async def deny(send, status: int) -> None:
    body = b'{"detail":"Connexion requise"}' if status == 401 else b'{"detail":"Acces refuse"}'
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


async def allowed(scope, role: str) -> int:
    """Équivalent de l'auth_request Nginx : renvoie 204, 401 ou 403."""
    headers = {k.decode(): v.decode() for k, v in scope["headers"]
               if k.lower() in (b"cookie", b"authorization", b"x-api-key")}
    r = await orders_client.get("/api/auth/check", params={"role": role}, headers=headers)
    return r.status_code


async def gateway(scope, receive, send):
    if scope["type"] == "lifespan":
        msg = await receive()
        ctx = lifespan()
        await ctx.__aenter__()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await ctx.__aexit__(None, None, None)
        await send({"type": "lifespan.shutdown.complete"})
        return
    path = scope["path"]
    if scope["type"] == "http":
        need = "any" if path.startswith(("/api/catalog", "/api/availability")) else \
            "staff" if path.startswith(("/erp/", "/boutiques")) or path in (
                "/svc/notifications/notifications", "/svc/connector-rest/stats", "/svc/connector-csv/stats") else None
        if need and (code := await allowed(scope, need)) != 204:
            return await deny(send, code)
        if path.startswith("/svc/") and "/internal/" in path:
            return await deny(send, 403)
    if path.startswith(("/api/catalog", "/api/availability")):
        return await stock(scope, receive, send)
    if path.startswith("/api/"):
        return await orders(scope, receive, send)
    if path.startswith("/erp/"):
        return await erp(scope, receive, send)
    for prefix, app in PREFIXED.items():
        if path == prefix or path.startswith(prefix + "/"):
            return await app({**scope, "root_path": prefix}, receive, send)
    return await static(scope, receive, send)


if __name__ == "__main__":
    print("Portail : http://localhost:8080  ·  API : http://localhost:8080/svc/orders/docs")
    uvicorn.run(gateway, host="0.0.0.0", port=8080, log_level="warning")
