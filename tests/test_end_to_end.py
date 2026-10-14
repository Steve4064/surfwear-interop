"""Scénario de bout en bout : de la commande en ligne à la livraison en boutique."""
from __future__ import annotations

import asyncio
import csv

import pytest

from conftest import key

pytestmark = pytest.mark.asyncio


def line(order: dict, sku: str) -> dict:
    return next(l for l in order["lines"] if l["sku"] == sku)


async def test_catalog_and_availability(eco):
    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 5
    assert cat["SW-LYC-004"]["upcoming"] == [{"eta": "2026-11-02", "available": 30}]

    av = (await eco.stock.get("/api/availability/SW-LYC-004", params={"qty": 12})).json()
    assert av["can_ship_now"] is False
    assert av["complete_by"] == "2026-11-02"
    assert [(p["qty"], p["source"]) for p in av["plan"]] == [(5, "stock"), (7, "lot")]

    av = (await eco.stock.get("/api/availability/SW-TSH-001", params={"qty": 10})).json()
    assert av["can_ship_now"] is True
    assert (await eco.stock.get("/api/availability/NOPE")).status_code == 404


async def test_security_and_validation(eco):
    assert (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-TSH-001", "qty": 1}]},
                                  headers={"X-API-Key": "bad"})).status_code == 401
    r = await eco.orders.post("/api/orders", json={"lines": [{"sku": "NOPE", "qty": 1}]}, headers=key("biarritz"))
    assert r.status_code == 422
    r = await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-TSH-001", "qty": 0}]}, headers=key("biarritz"))
    assert r.status_code == 422


async def test_full_flow(eco):
    # 1. Biarritz (éditeur A) commande plus que le stock disponible de lycras
    body = {"lines": [{"sku": "SW-LYC-004", "qty": 12}, {"sku": "SW-TSH-001", "qty": 3}], "confirm": True}
    r = await eco.orders.post("/api/orders", json=body, headers={**key("biarritz"), "Idempotency-Key": "k1"})
    assert r.status_code == 201
    bia = r.json()
    assert bia["status"] == "backordered" and bia["state"] == "confirmed"
    assert line(bia, "SW-LYC-004")["qty_reserved"] == 5
    assert line(bia, "SW-LYC-004")["scheduled"][0]["qty"] == 7
    assert line(bia, "SW-TSH-001")["qty_reserved"] == 3

    # Rejouer la même requête ne crée pas de doublon
    r2 = await eco.orders.post("/api/orders", json=body, headers={**key("biarritz"), "Idempotency-Key": "k1"})
    assert r2.status_code == 200 and r2.json()["order_id"] == bia["order_id"]

    # 2. Hossegor veut 30 lycras : plus de stock, 23 sur le lot, 7 sans date
    hos = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-LYC-004", "qty": 30}], "confirm": True},
                                 headers=key("hossegor"))).json()
    l = line(hos, "SW-LYC-004")
    assert (l["qty_reserved"], sum(s["qty"] for s in l["scheduled"]), l["qty_unscheduled"]) == (0, 23, 7)

    # 3. Lacanau (éditeur B, CSV) commande des ponchos, en rupture mais attendus
    lac = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-PON-005", "qty": 2}], "confirm": True},
                                 headers=key("lacanau"))).json()
    assert line(lac, "SW-PON-005")["scheduled"][0]["eta"] == "2026-11-09"
    await eco.bus.drain()

    # Plus aucune double réservation : le stock affiché tient compte des promesses
    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 0 and cat["SW-LYC-004"]["upcoming"] == []

    # La liste de préparation ne contient que du stock réservé
    picks = (await eco.erp.get("/erp/pickings")).json()
    assert [p["order_id"] for p in picks] == [bia["order_id"]]
    assert {x["sku"]: x["qty"] for x in picks[0]["lines"]} == {"SW-LYC-004": 5, "SW-TSH-001": 3}

    # Chaque boutique a été informée dans le format de son éditeur
    msgs = (await eco.sim.get("/vendor-a/SHOP-BIA/messages")).json()
    assert [m["messageType"] for m in msgs] == ["ORDER_ACK"]
    assert msgs[0]["items"][0]["backorder"] == [{"quantity": 7, "expectedDate": "2026-11-02"}]
    files = list((eco.exchange / "SHOP-LAC" / "inbox").glob("*.csv"))
    assert len(files) == 1
    rows = list(csv.DictReader(files[0].open(encoding="utf-8", newline="")))
    assert rows[0]["record_type"] == "COMMANDE" and rows[0]["qty_planned"] == "2"
    notes = (await eco.notif.get("/notifications")).json()
    assert len(notes) == 3 and all("finalisée" in n["subject"] for n in notes)

    # 4. Un nouveau lot est annoncé : le reliquat sans date d'Hossegor est planifié
    lot = (await eco.erp.post("/erp/lots", json={"sku": "SW-LYC-004", "qty": 10, "eta": "2026-12-01"})).json()
    await eco.bus.drain()
    hos = (await eco.orders.get(f"/api/orders/{hos['order_id']}", headers=key("hossegor"))).json()
    l = line(hos, "SW-LYC-004")
    assert l["qty_unscheduled"] == 0
    assert [(s["qty"], s["eta"]) for s in l["scheduled"]] == [(23, "2026-11-02"), (7, "2026-12-01")]

    # 5. Le lot LOT-0001 arrive : les reliquats planifiés deviennent des réservations fermes
    await eco.erp.post("/erp/receptions", json={"lot_id": "LOT-0001"})
    await eco.bus.drain()
    bia = (await eco.orders.get(f"/api/orders/{bia['order_id']}", headers=key("biarritz"))).json()
    assert line(bia, "SW-LYC-004")["qty_reserved"] == 12 and bia["status"] == "confirmed"
    hos = (await eco.orders.get(f"/api/orders/{hos['order_id']}", headers=key("hossegor"))).json()
    assert line(hos, "SW-LYC-004")["qty_reserved"] == 23

    # 6. Le logisticien expédie : bons de livraison, stock consommé, boutiques à jour
    shipments = (await eco.erp.post("/erp/pickings/dispatch", json={})).json()
    assert {s["order_id"] for s in shipments} == {bia["order_id"], hos["order_id"]}
    await eco.bus.drain()

    bia = (await eco.orders.get(f"/api/orders/{bia['order_id']}", headers=key("biarritz"))).json()
    assert bia["status"] == "shipped"
    hos = (await eco.orders.get(f"/api/orders/{hos['order_id']}", headers=key("hossegor"))).json()
    assert hos["status"] == "partially_shipped"

    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 0
    erp_stock = {p["sku"]: p["on_hand"] for p in (await eco.erp.get("/erp/stock")).json()["products"]}
    assert erp_stock["SW-LYC-004"] == 0  # 5 + 30 reçus - 12 - 23 expédiés

    assert (await eco.sim.get("/vendor-a/SHOP-BIA/stock")).json() == {"SW-LYC-004": 12, "SW-TSH-001": 3}
    assert (await eco.sim.get("/vendor-a/SHOP-HOS/stock")).json() == {"SW-LYC-004": 23}
    subjects = [n["subject"] for n in (await eco.notif.get("/notifications")).json()]
    assert sum("expédiée" in s for s in subjects) == 2
    assert eco.bus.failures == []

    # Les connecteurs n'ont traité que les boutiques de leur éditeur
    assert (await eco.conn_csv.get("/stats")).json()["delivered"] == 1
    assert (await eco.conn_rest.get("/stats")).json()["delivered"] >= 6


async def test_concurrent_orders_never_oversell(eco):
    """Deux boutiques commandent en même temps les 12 derniers leashs (aucun lot prévu)."""
    body = {"lines": [{"sku": "SW-LEA-008", "qty": 12}]}
    r1, r2 = await asyncio.gather(
        eco.orders.post("/api/orders", json=body, headers=key("biarritz")),
        eco.orders.post("/api/orders", json=body, headers=key("arcachon")),
    )
    l1, l2 = line(r1.json(), "SW-LEA-008"), line(r2.json(), "SW-LEA-008")
    assert l1["qty_reserved"] + l2["qty_reserved"] == 12
    assert l1["qty_unscheduled"] + l2["qty_unscheduled"] == 12
