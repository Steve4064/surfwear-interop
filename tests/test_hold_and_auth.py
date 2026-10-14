"""Pré-réservation, finalisation, expiration, annulation et connexion."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from conftest import key
from order_service.models import Order, utcnow_naive

pytestmark = pytest.mark.asyncio


async def available(eco, sku: str) -> int:
    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    return cat[sku]["available_now"]


async def expire(eco, order_id: str) -> None:
    """Simule le passage du temps : l'échéance de la pré-réservation est dépassée."""
    async with eco.orders_app.state.session.begin() as s:
        await s.execute(update(Order).where(Order.id == order_id)
                        .values(expires_at=utcnow_naive() - timedelta(seconds=1)))


async def login(eco, username: str, password: str):
    return await eco.orders.post("/api/auth/login", json={"username": username, "password": password})


async def test_pending_order_holds_stock_until_finalized(eco):
    r = await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 10}]},
                              headers=key("biarritz"))
    order = r.json()
    assert order["state"] == "pending" and order["status"] == "pending"
    assert 0 < order["seconds_left"] <= 15 * 60
    assert await available(eco, "SW-LEA-008") == 2  # bloqué pour les autres boutiques

    await eco.bus.drain()
    assert (await eco.erp.get("/erp/pickings")).json() == []  # rien ne part à l'entrepôt
    assert (await eco.notif.get("/notifications")).json() == []

    r = await eco.orders.post(f"/api/orders/{order['order_id']}/confirm", headers=key("biarritz"))
    assert r.status_code == 200 and r.json()["state"] == "confirmed"
    await eco.bus.drain()
    assert len((await eco.erp.get("/erp/pickings")).json()) == 1
    # Finaliser deux fois ne republie rien
    again = await eco.orders.post(f"/api/orders/{order['order_id']}/confirm", headers=key("biarritz"))
    assert again.status_code == 200
    await eco.bus.drain()
    assert len((await eco.notif.get("/notifications")).json()) == 1


async def test_expired_hold_frees_stock_for_other_shop(eco):
    # Biarritz bloque les 12 leashs mais ne finalise pas
    bia = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 12}]},
                                 headers=key("biarritz"))).json()
    # Arcachon commande quand même : tout part en reliquat et finalise
    arc = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 5}], "confirm": True},
                                 headers=key("arcachon"))).json()
    assert arc["lines"][0]["qty_unscheduled"] == 5

    await expire(eco, bia["order_id"])
    assert await eco.orders_app.state.sweep_once() == 1
    await eco.bus.drain()

    bia = (await eco.orders.get(f"/api/orders/{bia['order_id']}", headers=key("biarritz"))).json()
    assert bia["state"] == "expired" and bia["seconds_left"] is None
    # Le stock libéré a servi en priorité le reliquat d'Arcachon
    arc = (await eco.orders.get(f"/api/orders/{arc['order_id']}", headers=key("arcachon"))).json()
    assert arc["lines"][0]["qty_reserved"] == 5 and arc["status"] == "confirmed"
    assert await available(eco, "SW-LEA-008") == 7
    picks = (await eco.erp.get("/erp/pickings")).json()
    assert [(p["order_id"], p["lines"][0]["qty"]) for p in picks] == [(arc["order_id"], 5)]

    # Trop tard pour finaliser
    r = await eco.orders.post(f"/api/orders/{bia['order_id']}/confirm", headers=key("biarritz"))
    assert r.status_code == 409


async def test_finalize_after_deadline_is_refused_even_before_sweep(eco):
    o = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-CAS-006", "qty": 4}]},
                               headers=key("hossegor"))).json()
    await expire(eco, o["order_id"])
    r = await eco.orders.post(f"/api/orders/{o['order_id']}/confirm", headers=key("hossegor"))
    assert r.status_code == 409
    assert await available(eco, "SW-CAS-006") == 60


async def test_cancel_releases_lot_and_stock(eco):
    o = (await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-LYC-004", "qty": 12}]},
                               headers=key("lacanau"))).json()
    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 0 and cat["SW-LYC-004"]["upcoming"][0]["available"] == 23

    r = await eco.orders.post(f"/api/orders/{o['order_id']}/cancel", headers=key("lacanau"))
    assert r.json()["state"] == "cancelled"
    cat = {p["sku"]: p for p in (await eco.stock.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 5 and cat["SW-LYC-004"]["upcoming"][0]["available"] == 30
    # Une autre boutique ne peut ni voir ni annuler
    assert (await eco.orders.post(f"/api/orders/{o['order_id']}/cancel", headers=key("biarritz"))).status_code == 404


async def test_login_roles_and_access(eco):
    assert (await login(eco, "biarritz", "mauvais")).status_code == 401
    r = await login(eco, "Biarritz", "Biarritz2026!")
    assert r.status_code == 200 and r.json()["shop"]["id"] == "SHOP-BIA"
    assert "sw_session" in r.headers.get("set-cookie", "")
    bearer = {"Authorization": f"Bearer {r.json()['token']}"}

    assert (await eco.orders.get("/api/orders", headers=bearer)).status_code == 200
    assert (await eco.orders.get("/api/admin/orders", headers=bearer)).status_code == 403
    assert (await eco.orders.get("/api/auth/check", params={"role": "staff"}, headers=bearer)).status_code == 403
    assert (await eco.orders.get("/api/auth/check", headers=bearer)).status_code == 204
    assert (await eco.orders.get("/api/orders")).status_code == 200  # cookie de session du navigateur
    eco.orders.cookies.clear()
    assert (await eco.orders.get("/api/auth/check")).status_code == 401
    assert (await eco.orders.get("/api/orders")).status_code == 401

    staff = await login(eco, "surfwear", "Surfwear2026!")
    sb = {"Authorization": f"Bearer {staff.json()['token']}"}
    assert staff.json()["role"] == "staff"
    assert (await eco.orders.get("/api/auth/check", params={"role": "staff"}, headers=sb)).status_code == 204
    assert (await eco.orders.get("/api/admin/orders", headers=sb)).status_code == 200
    assert len((await eco.orders.get("/api/admin/shops", headers=sb)).json()) == 4
    # Surfwear ne passe pas de commande à la place d'une boutique
    r = await eco.orders.post("/api/orders", json={"lines": [{"sku": "SW-TSH-001", "qty": 1}]}, headers=sb)
    assert r.status_code == 403
    # Déconnexion puis jeton falsifié
    await eco.orders.post("/api/auth/logout")
    eco.orders.cookies.clear()
    assert (await eco.orders.get("/api/orders", headers={"Authorization": "Bearer abc.def"})).status_code == 401
