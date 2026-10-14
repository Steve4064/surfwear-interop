"""Pré-réservation : blocage du stock, finalisation, expiration, annulation."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from conftest import key
from order_service.models import Order, utcnow_naive

pytestmark = pytest.mark.asyncio


async def available(c, sku: str) -> int:
    return {p["sku"]: p for p in (await c.get("/api/catalog")).json()}[sku]["available_now"]


async def expire(eco, order_id: str) -> None:
    async with eco.orders_session.begin() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(expires_at=utcnow_naive() - timedelta(seconds=1)))


async def test_hold_blocks_stock_and_nothing_leaves_before_finalization(eco):
    bia = eco.client(headers=key("biarritz"))
    staff = await eco.login("equipe@surfwear.example")
    o = (await bia.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 10}]})).json()
    assert o["state"] == "pending" and 0 < o["seconds_left"] <= 900
    assert await available(bia, "SW-LEA-008") == 2
    await eco.bus.drain()
    assert (await staff.get("/api/warehouse/pickings")).json() == []
    await bia.post(f"/api/orders/{o['order_id']}/confirm")
    await bia.post(f"/api/orders/{o['order_id']}/confirm")  # idempotent
    await eco.bus.drain()
    assert len((await staff.get("/api/warehouse/pickings")).json()) == 1


async def test_expired_hold_serves_other_shops_backorder(eco):
    bia, arc = eco.client(headers=key("biarritz")), eco.client(headers=key("arcachon"))
    staff = await eco.login("equipe@surfwear.example")
    held = (await bia.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 12}]})).json()
    waiting = (await arc.post("/api/orders", json={"lines": [{"sku": "SW-LEA-008", "qty": 5}], "confirm": True})).json()
    assert waiting["lines"][0]["qty_unscheduled"] == 5
    await expire(eco, held["order_id"])
    assert await eco.sweep() == 1
    await eco.bus.drain()
    assert (await bia.get(f"/api/orders/{held['order_id']}")).json()["state"] == "expired"
    assert (await arc.get(f"/api/orders/{waiting['order_id']}")).json()["lines"][0]["qty_reserved"] == 5
    assert await available(arc, "SW-LEA-008") == 7
    assert [p["order_id"] for p in (await staff.get("/api/warehouse/pickings")).json()] == [waiting["order_id"]]
    assert (await bia.post(f"/api/orders/{held['order_id']}/confirm")).status_code == 409


async def test_cancel_releases_stock_and_lot(eco):
    lac = eco.client(headers=key("lacanau"))
    o = (await lac.post("/api/orders", json={"lines": [{"sku": "SW-LYC-004", "qty": 12}]})).json()
    assert await available(lac, "SW-LYC-004") == 0
    assert (await lac.post(f"/api/orders/{o['order_id']}/cancel")).json()["state"] == "cancelled"
    cat = {p["sku"]: p for p in (await lac.get("/api/catalog")).json()}
    assert cat["SW-LYC-004"]["available_now"] == 5 and cat["SW-LYC-004"]["upcoming"][0]["available"] == 30
    other = eco.client(headers=key("biarritz"))
    assert (await other.post(f"/api/orders/{o['order_id']}/cancel")).status_code == 404


async def test_concurrent_orders_never_oversell(eco):
    a, b = eco.client(headers=key("biarritz")), eco.client(headers=key("arcachon"))
    body = {"lines": [{"sku": "SW-LEA-008", "qty": 12}], "confirm": True}
    r1, r2 = await asyncio.gather(a.post("/api/orders", json=body), b.post("/api/orders", json=body))
    l1, l2 = r1.json()["lines"][0], r2.json()["lines"][0]
    assert l1["qty_reserved"] + l2["qty_reserved"] == 12
