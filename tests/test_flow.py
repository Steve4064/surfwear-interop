"""Parcours complet : catalogue → panier → pré-réservation → finalisation → expédition → facture."""
from __future__ import annotations

import csv

import pytest

from conftest import key

pytestmark = pytest.mark.asyncio


def line(order: dict, sku: str) -> dict:
    return next(l for l in order["lines"] if l["sku"] == sku)


async def test_shop_journey_with_cart_invoice_and_both_vendors(eco):
    bia = await eco.login("biarritz@boutiques.example")
    staff = await eco.login("equipe@surfwear.example")

    # Catalogue : fiches produits + disponibilité (deux services)
    cat = {p["sku"]: p for p in (await bia.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 5 and cat["SW-LYC-004"]["description"]
    assert cat["SW-LYC-004"]["upcoming"] == [{"eta": "2026-11-02", "available": 30}]
    assert (await bia.get("/api/catalog", params={"category": "Bain"})).json()[0]["category"] == "Bain"

    # Panier côté serveur, avec aperçu de livraison et TVA
    await bia.put("/api/cart/items/SW-LYC-004", json={"qty": 12})
    cart = (await bia.put("/api/cart/items/SW-TSH-001", json={"qty": 3})).json()
    assert cart["count"] == 15 and cart["total_ht"] == 331.5 and cart["total_ttc"] == 397.8
    lyc = next(l for l in cart["lines"] if l["sku"] == "SW-LYC-004")
    assert lyc["plan"] == {"now": 5, "later": [{"qty": 7, "eta": "2026-11-02"}], "unscheduled": 0}
    # Le panier suit l'utilisateur (nouvelle session, même compte)
    again = await eco.login("biarritz@boutiques.example")
    assert (await again.get("/api/cart")).json()["count"] == 15

    # Validation du panier = pré-réservation ; le panier est vidé
    order = (await bia.post("/api/cart/checkout", headers={"Idempotency-Key": "k1"})).json()
    assert order["state"] == "pending" and order["placed_by"] == "Léa Etcheverry"
    assert (await bia.get("/api/cart")).json()["count"] == 0
    assert (await bia.post("/api/cart/checkout")).status_code == 409

    order = (await bia.post(f"/api/orders/{order['order_id']}/confirm")).json()
    assert order["status"] == "backordered"

    # Lacanau (éditeur B) commande par son logiciel (clé d'API) et finalise en un appel
    lac = eco.client(headers=key("lacanau"))
    r = await lac.post("/api/orders", json={"lines": [{"sku": "SW-PON-005", "qty": 2}], "confirm": True})
    assert r.status_code == 201
    await eco.bus.drain()

    # Entrepôt : réception du lot, préparation, expédition
    picks = (await staff.get("/api/warehouse/pickings")).json()
    assert [(p["order_id"], {l["sku"]: l["qty"] for l in p["lines"]}) for p in picks] == \
        [(order["order_id"], {"SW-LYC-004": 5, "SW-TSH-001": 3})]
    await staff.post("/api/warehouse/receptions", json={"lot_id": "LOT-0001", "reference": "BL-FOURN-77"})
    await eco.bus.drain()
    shipped = (await staff.post("/api/warehouse/pickings/dispatch", json={})).json()
    assert len(shipped) == 1 and {l["sku"]: l["qty"] for l in shipped[0]["lines"]} == {"SW-LYC-004": 12, "SW-TSH-001": 3}
    await eco.bus.drain()
    assert (await bia.get(f"/api/orders/{order['order_id']}")).json()["status"] == "shipped"
    recs = (await staff.get("/api/warehouse/receptions")).json()
    assert recs[0]["reference"] == "BL-FOURN-77" and recs[0]["received_by"] == "Équipe Surfwear"

    # Facture émise automatiquement, téléchargeable en PDF par la boutique uniquement
    invoices = (await bia.get("/api/invoices")).json()
    assert len(invoices) == 1
    inv = invoices[0]
    assert inv["id"].startswith("FAC-") and inv["total_ht"] == 331.5 and inv["total_ttc"] == 397.8
    assert inv["shop"]["siret"] == "81234567800012"
    pdf = await bia.get(f"/api/invoices/{inv['id']}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and len(pdf.content) > 1500
    assert "attachment" in pdf.headers["content-disposition"]
    hos = await eco.login("hossegor@boutiques.example")
    assert (await hos.get(f"/api/invoices/{inv['id']}/pdf")).status_code == 404
    assert (await hos.get("/api/invoices")).json() == []
    paid = (await staff.post(f"/api/invoices/{inv['id']}/paid")).json()
    assert paid["status"] == "paid"
    assert len((await staff.get("/api/invoices")).json()) == 1

    # Les boutiques ont été informées dans le format de leur éditeur, et par mail
    sim = eco.apps["simulator"].state
    assert [m["messageType"] for m in sim.messages["SHOP-BIA"]] == ["ORDER_ACK", "ORDER_CHANGE", "DELIVERY_NOTE"]
    assert dict(sim.stock["SHOP-BIA"]) == {"SW-LYC-004": 12, "SW-TSH-001": 3}
    files = list((eco.exchange / "SHOP-LAC" / "inbox").glob("*.csv"))
    rows = list(csv.DictReader(files[0].open(encoding="utf-8", newline="")))
    assert rows[0]["record_type"] == "COMMANDE" and rows[0]["qty_planned"] == "2"
    subjects = [n["subject"] for n in (await staff.get("/svc/notifications/notifications")).json()]
    assert any(s.startswith("Facture FAC-") for s in subjects)
    assert eco.bus.failures == []
