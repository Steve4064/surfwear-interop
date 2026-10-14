"""Adaptateurs par éditeur de logiciel de boutique (patron Adaptateur).

Chaque adaptateur reçoit un événement canonique du bus et le traduit dans le
format attendu par le logiciel de la boutique. Ajouter un éditeur = écrire un
adaptateur, sans modifier les autres services.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from surfwear_common.events import ORDER_CONFIRMED, ORDER_UPDATED, SHIPMENT_DISPATCHED, Event

log = logging.getLogger("shop-connector.adapters")


def shop_of(event: Event) -> dict:
    return event.data["shop"]


class Adapter(Protocol):
    name: str

    async def deliver(self, event: Event) -> str: ...


# --------------------------------------------------------------------------- #
# Éditeur A : logiciel moderne, webhooks JSON signés                           #
# --------------------------------------------------------------------------- #
VENDOR_A_TYPES = {
    ORDER_CONFIRMED: "ORDER_ACK",
    ORDER_UPDATED: "ORDER_CHANGE",
    SHIPMENT_DISPATCHED: "DELIVERY_NOTE",
}


def to_vendor_a(event: Event) -> dict:
    d = event.data
    msg = {
        "messageType": VENDOR_A_TYPES[event.type],
        "messageId": event.id,
        "sentAt": event.occurred_at.isoformat(),
        "storeCode": d["shop"]["id"],
        "purchaseOrder": d["order_id"],
    }
    if event.type == SHIPMENT_DISPATCHED:
        msg["deliveryNote"] = d["shipment_id"]
        msg["items"] = [{"itemCode": l["sku"], "label": l["name"], "quantity": l["qty"]} for l in d["lines"]]
    else:
        msg["status"] = d["status"]
        msg["items"] = [
            {
                "itemCode": l["sku"],
                "label": l["name"],
                "unitPrice": l["unit_price"],
                "ordered": l["qty_ordered"],
                "confirmed": l["qty_reserved"] + l["qty_shipped"],
                "backorder": [{"quantity": s["qty"], "expectedDate": s["eta"]} for s in l["scheduled"]],
                "unscheduled": l["qty_unscheduled"],
            }
            for l in d["lines"]
        ]
    return msg


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class VendorARestAdapter:
    name = "rest"

    def __init__(self, client: httpx.AsyncClient, secret: str, retries: int = 3):
        self.client = client
        self.secret = secret
        self.retries = retries

    async def deliver(self, event: Event) -> str:
        shop_id = shop_of(event)["id"]
        body = json.dumps(to_vendor_a(event), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "X-Surfwear-Signature": sign(self.secret, body)}
        for attempt in range(1, self.retries + 1):
            try:
                r = await self.client.post(f"/vendor-a/webhooks/{shop_id}", content=body, headers=headers)
                r.raise_for_status()
                return f"webhook {shop_id} HTTP {r.status_code}"
            except httpx.HTTPError as exc:
                log.warning("Webhook %s en échec (%s/%s) : %s", shop_id, attempt, self.retries, exc)
                if attempt == self.retries:
                    raise  # le message part en file de lettres mortes
                await asyncio.sleep(0.5 * 2 ** attempt)
        raise RuntimeError("inatteignable")


# --------------------------------------------------------------------------- #
# Éditeur B : logiciel historique, import de fichiers CSV                      #
# --------------------------------------------------------------------------- #
CSV_HEADER = [
    "record_type", "order_id", "sku", "label", "qty_ordered", "qty_confirmed",
    "qty_planned", "expected_date", "qty_unscheduled", "qty_shipped", "delivery_note",
]


def to_vendor_b_rows(event: Event) -> list[list]:
    d = event.data
    if event.type == SHIPMENT_DISPATCHED:
        return [["LIVRAISON", d["order_id"], l["sku"], l["name"], "", "", "", "", "", l["qty"], d["shipment_id"]]
                for l in d["lines"]]
    record = "COMMANDE" if event.type == ORDER_CONFIRMED else "MODIFICATION"
    rows = []
    for l in d["lines"]:
        planned = sum(s["qty"] for s in l["scheduled"])
        etas = [s["eta"] for s in l["scheduled"] if s["eta"]]
        rows.append([record, d["order_id"], l["sku"], l["name"], l["qty_ordered"],
                     l["qty_reserved"] + l["qty_shipped"], planned, max(etas) if etas else "",
                     l["qty_unscheduled"], l["qty_shipped"], ""])
    return rows


def to_vendor_b_csv(event: Event) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")  # RFC 4180
    writer.writerow(CSV_HEADER)
    writer.writerows(to_vendor_b_rows(event))
    return buf.getvalue()


class VendorBCsvAdapter:
    name = "csv"

    def __init__(self, exchange_dir: str):
        self.root = Path(exchange_dir)

    async def deliver(self, event: Event) -> str:
        shop_id = shop_of(event)["id"]
        inbox = self.root / shop_id / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        name = f"{stamp}_{event.type.replace('.', '-')}_{event.data['order_id']}.csv"
        tmp = inbox / f".{name}.tmp"
        tmp.write_text(to_vendor_b_csv(event), encoding="utf-8")
        os.replace(tmp, inbox / name)  # écriture atomique : le logiciel ne lit jamais un fichier partiel
        return f"fichier {shop_id}/inbox/{name}"
