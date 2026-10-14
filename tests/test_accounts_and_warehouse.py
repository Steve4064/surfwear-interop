"""Inscription B2B et validation, contrôle d'accès, ajout de produit, réception, inventaire."""
from __future__ import annotations

import pytest

from conftest import key

pytestmark = pytest.mark.asyncio

SIGNUP = {"company": "Capbreton Surf Co", "siret": "123 456 789 00012", "address": "2 quai de la Pêcherie",
          "city": "Capbreton", "first_name": "Jon", "last_name": "Darrigade",
          "email": "jon@capbreton-surf.example", "password": "Capbreton2026!"}


async def test_registration_needs_staff_approval(eco):
    anon = eco.client()
    r = await anon.post("/api/register", json=SIGNUP)
    assert r.status_code == 201 and r.json()["status"] == "pending"
    shop_id = r.json()["shop_id"]
    assert (await anon.post("/api/register", json=SIGNUP)).status_code == 409  # même SIRET
    bad = {**SIGNUP, "siret": "123", "password": "faible"}
    assert (await anon.post("/api/register", json=bad)).status_code == 422

    # La boutique se connecte mais ne peut ni consulter ni commander avant validation
    new = eco.client()
    r = await new.post("/api/auth/login", json={"username": SIGNUP["email"], "password": SIGNUP["password"]})
    assert r.json()["shop"]["status"] == "pending"
    assert (await new.get("/api/catalog")).status_code == 403
    assert (await new.put("/api/cart/items/SW-TSH-001", json={"qty": 1})).status_code == 403

    staff = await eco.login("equipe@surfwear.example")
    shops = (await staff.get("/api/admin/shops")).json()
    assert shops[0]["id"] == shop_id and shops[0]["status"] == "pending"  # les demandes en premier
    await eco.bus.drain()
    notes = (await staff.get("/svc/notifications/notifications")).json()
    assert notes[0]["to"] == "equipe@surfwear.example" and "Capbreton Surf Co" in notes[0]["subject"]

    approved = (await staff.post(f"/api/admin/shops/{shop_id}/approve", json={"connector": "csv"})).json()
    assert approved["status"] == "active" and approved["api_key"].startswith("sk_")
    assert (await new.get("/api/catalog")).status_code == 200
    r = await new.post("/api/orders", json={"lines": [{"sku": "SW-TSH-001", "qty": 2}], "confirm": True})
    assert r.status_code == 201 and r.json()["shop"]["connector"] == "csv"
    # La clé d'API délivrée fonctionne pour le logiciel de la boutique
    assert (await eco.client(headers={"X-API-Key": approved["api_key"]}).get("/api/orders")).status_code == 200

    hos = await eco.login("hossegor@boutiques.example")
    assert (await hos.get("/api/admin/shops")).status_code == 403
    assert (await hos.post(f"/api/admin/shops/{shop_id}/suspend")).status_code == 403
    await staff.post(f"/api/admin/shops/{shop_id}/suspend")
    assert (await new.get("/api/catalog")).status_code == 403


async def test_access_control_and_header_spoofing(eco):
    anon = eco.client()
    for path in ("/api/catalog", "/api/cart", "/api/orders", "/api/invoices", "/api/warehouse/pickings", "/api/products"):
        assert (await anon.get(path)).status_code == 401, path
    # Un client qui forge les en-têtes d'identité est ignoré
    forged = eco.client(headers={"X-Auth-User": "x", "X-Auth-Role": "staff", "X-Auth-Shop": "SHOP-BIA"})
    assert (await forged.get("/api/warehouse/pickings")).status_code == 401
    bia = await eco.login("biarritz@boutiques.example")
    bia.headers.update({"X-Auth-Role": "staff", "X-Auth-Shop": "SHOP-HOS"})
    assert (await bia.get("/api/warehouse/pickings")).status_code == 403
    assert (await bia.get("/api/products")).status_code == 403
    await bia.put("/api/cart/items/SW-TSH-001", json={"qty": 1})
    o = (await bia.post("/api/cart/checkout")).json()
    assert o["shop"]["id"] == "SHOP-BIA"  # pas SHOP-HOS
    # Les routes internes ne sont jamais exposées
    assert (await anon.get("/svc/stock/internal/stock")).status_code == 404
    assert (await anon.get("/internal/shops/SHOP-BIA")).status_code == 404
    assert (await anon.get("/svc/catalog/docs")).status_code == 200
    bad = eco.client()
    assert (await bad.post("/api/auth/login", json={"username": "biarritz@boutiques.example",
                                                    "password": "faux"})).status_code == 401


async def test_staff_adds_product_receives_and_counts_stock(eco):
    staff = await eco.login("equipe@surfwear.example")
    bia = await eco.login("biarritz@boutiques.example")
    new = {"sku": "sw-par-009", "name": "Parka coupe-vent Seignosse", "category": "Textile",
           "description": "Nylon ripstop, capuche roulée dans le col.", "unit_price": 48.0}
    r = await staff.post("/api/products", json=new)
    assert r.status_code == 201 and r.json()["sku"] == "SW-PAR-009"
    assert (await staff.post("/api/products", json=new)).status_code == 409
    assert (await bia.post("/api/products", json={**new, "sku": "SW-X-1"})).status_code == 403
    await eco.bus.drain()

    cat = {p["sku"]: p for p in (await bia.get("/api/catalog")).json()}
    assert cat["SW-PAR-009"]["available_now"] == 0  # créé dans le Stock par événement

    # Réception libre (sans lot annoncé), puis inventaire
    await staff.post("/api/warehouse/receptions", json={"sku": "SW-PAR-009", "qty": 30, "reference": "BL-4412"})
    await eco.bus.drain()
    assert {p["sku"]: p for p in (await bia.get("/api/catalog")).json()}["SW-PAR-009"]["available_now"] == 30
    await bia.put("/api/cart/items/SW-PAR-009", json={"qty": 8})
    o = (await bia.post("/api/cart/checkout")).json()
    await bia.post(f"/api/orders/{o['order_id']}/confirm")
    r = await staff.post("/api/warehouse/adjustments", json={"sku": "SW-PAR-009", "on_hand": 26, "reason": "Casse"})
    assert r.json() == {"sku": "SW-PAR-009", "before": 30, "after": 26}
    await eco.bus.drain()
    assert {p["sku"]: p for p in (await bia.get("/api/catalog")).json()}["SW-PAR-009"]["available_now"] == 18
    # Désactivé : disparaît du catalogue des boutiques
    await staff.patch("/api/products/SW-PAR-009", json={"active": False, "unit_price": 45.0})
    assert "SW-PAR-009" not in {p["sku"] for p in (await bia.get("/api/catalog")).json()}
    hist = (await staff.get("/api/warehouse/adjustments")).json()
    assert hist[0]["before"] == 30 and hist[0]["after"] == 26 and hist[0]["by"] == "Équipe Surfwear"
