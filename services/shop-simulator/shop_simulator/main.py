"""Simulateur des logiciels de boutique des deux éditeurs.

* Éditeur A (moderne) : reçoit des webhooks JSON signés, met à jour le stock de
  la boutique à réception d'un bon de livraison.
* Éditeur B (historique) : lit les fichiers CSV déposés dans le répertoire
  d'échange de chaque boutique (simulation de l'import).

Ce service n'existe que pour la démonstration : en production, ce sont les
logiciels réels des boutiques qui reçoivent ces messages.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
from collections import defaultdict
from html import escape
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from surfwear_common.service import env, setup_logging

SERVICE = "shop-simulator"
log = setup_logging(SERVICE)


def create_app(secret: str | None = None, exchange_dir: str | None = None) -> FastAPI:
    secret = secret or env("WEBHOOK_SECRET", "change-me")
    root = Path(exchange_dir or env("EXCHANGE_DIR", "/exchange"))
    messages: dict[str, list[dict]] = defaultdict(list)
    stock: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Simulateur des logiciels de boutique", version="1.0.0")
    app.state.messages = messages
    app.state.stock = stock

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": SERVICE}

    # ------------------------------------------------------------- éditeur A
    @app.post("/vendor-a/webhooks/{shop_id}", tags=["éditeur A"])
    async def webhook(shop_id: str, request: Request, x_surfwear_signature: str = Header("")):
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_surfwear_signature):
            raise HTTPException(401, "Signature invalide")
        msg = json.loads(body)
        if any(m["messageId"] == msg["messageId"] for m in messages[shop_id]):
            return {"status": "duplicate"}
        messages[shop_id].append(msg)
        if msg["messageType"] == "DELIVERY_NOTE":
            for item in msg["items"]:
                stock[shop_id][item["itemCode"]] += item["quantity"]
        log.info("[éditeur A] %s reçu par %s (%s)", msg["messageType"], shop_id, msg["purchaseOrder"])
        return {"status": "accepted"}

    @app.get("/vendor-a/{shop_id}/messages", tags=["éditeur A"])
    async def vendor_a_messages(shop_id: str):
        return messages[shop_id]

    @app.get("/vendor-a/{shop_id}/stock", tags=["éditeur A"])
    async def vendor_a_stock(shop_id: str):
        return stock[shop_id]

    # ------------------------------------------------------------- éditeur B
    def read_files(shop_id: str) -> list[dict]:
        inbox = root / shop_id / "inbox"
        if not inbox.exists():
            return []
        out = []
        for f in sorted(inbox.glob("*.csv")):
            with f.open(encoding="utf-8", newline="") as fh:
                out.append({"file": f.name, "rows": list(csv.DictReader(fh))})
        return out

    @app.get("/vendor-b/{shop_id}/files", tags=["éditeur B"])
    async def vendor_b_files(shop_id: str):
        return read_files(shop_id)

    # ------------------------------------------------------------- vue HTML
    @app.get("/", response_class=HTMLResponse)
    async def index():
        parts = ["<h1>Logiciels des boutiques (simulation)</h1><h2>Éditeur A : webhooks reçus</h2>"]
        for shop_id, msgs in messages.items():
            parts.append(f"<h3>{escape(shop_id)}</h3><ul>")
            parts += [f"<li>{escape(m['messageType'])} · {escape(m['purchaseOrder'])}</li>" for m in msgs]
            parts.append(f"</ul><p>Stock boutique : {escape(json.dumps(stock[shop_id]))}</p>")
        parts.append("<h2>Éditeur B : fichiers déposés</h2>")
        if root.exists():
            for shop_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                parts.append(f"<h3>{escape(shop_dir.name)}</h3><ul>")
                parts += [f"<li>{escape(f['file'])} ({len(f['rows'])} ligne(s))</li>" for f in read_files(shop_dir.name)]
                parts.append("</ul>")
        style = "<style>body{font-family:system-ui;margin:2rem;max-width:60rem}h3{margin-bottom:.2rem}</style>"
        return "<!doctype html><meta charset=utf-8><title>Boutiques</title>" + style + "".join(parts)

    return app
