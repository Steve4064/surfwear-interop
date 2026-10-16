"""Mode démonstration sans Docker.

Lance tout l'écosystème dans un seul processus Python : mêmes microservices, mêmes
appels entre services, mêmes événements, même contrôle d'accès que l'API Gateway (règles de gateway/kong.yml),
mais avec un bus en mémoire, SQLite et des comptes locaux à la place de ZITADEL.

    pip install -r scripts/requirements-dev.txt
    python scripts/dev_server.py          # puis http://localhost:8080

Les données sont stockées dans ./.devdata (supprimer le dossier pour repartir à zéro).
"""
from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from local_gateway import build_gateway  # noqa: E402
from local_stack import ROOT, build  # noqa: E402
from surfwear_common.bus import MemoryBus  # noqa: E402

DATA = ROOT / ".devdata"
DATA.mkdir(exist_ok=True)

bus = MemoryBus()
apps, _ = build(DATA, bus, hold_minutes=float(os.environ.get("HOLD_MINUTES", "15")), sweep_seconds=5)
gateway = build_gateway(apps, ROOT / "portal")


@asynccontextmanager
async def lifespan():
    async with AsyncExitStack() as stack:
        for name in ("warehouse", "stock", "identity", "catalog", "orders", "cart", "billing",
                     "conn_rest", "conn_csv", "notifications", "mcp"):
            await stack.enter_async_context(apps[name].router.lifespan_context(apps[name]))
        yield


async def server(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        ctx = lifespan()
        await ctx.__aenter__()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await ctx.__aexit__(None, None, None)
        await send({"type": "lifespan.shutdown.complete"})
        return
    await gateway(scope, receive, send)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"Portail : http://localhost:{port}  ·  Serveur MCP : http://localhost:{port}/mcp  ·  "
          f"Documentation : http://localhost:{port}/svc/orders/docs")
    uvicorn.run(server, host="0.0.0.0", port=port, log_level="warning")
