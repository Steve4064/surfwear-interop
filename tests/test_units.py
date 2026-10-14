"""Tests unitaires : routage du bus, idempotence, formats des éditeurs."""
from __future__ import annotations

import csv
import io

import pytest

from shop_connector.adapters import CSV_HEADER, sign, to_vendor_a, to_vendor_b_csv
from surfwear_common.bus import MemoryBus, topic_matches
from surfwear_common.events import ORDER_CONFIRMED, SHIPMENT_DISPATCHED, Event

SHOP = {"id": "SHOP-LAC", "name": "Lacanau Glisse", "connector": "csv", "email": "x@example.com"}
ORDER = {
    "order_id": "CMD-1", "shop": SHOP, "status": "backordered", "created_at": "2026-10-14T10:00:00Z",
    "total_amount": 58.0, "change": None,
    "lines": [{"sku": "SW-PON-005", "name": "Poncho, éponge", "unit_price": 29.0, "qty_ordered": 2,
               "qty_reserved": 0, "scheduled": [{"qty": 2, "eta": "2026-11-09", "lot_id": "LOT-0002"}],
               "qty_unscheduled": 0, "qty_shipped": 0}],
}


@pytest.mark.parametrize("pattern,key,expected", [
    ("order.*", "order.confirmed", True),
    ("order.*", "order.confirmed.v2", False),
    ("order.#", "order.confirmed.v2", True),
    ("#", "stock.received", True),
    ("stock.received", "stock.incoming", False),
])
def test_topic_matches(pattern, key, expected):
    assert topic_matches(pattern, key) is expected


async def test_memory_bus_routes_and_deduplicates():
    bus, got = MemoryBus(), []

    async def handler(e):
        got.append(e.type)

    bus.subscribe("q", ["order.*"], handler)
    event = Event(type=ORDER_CONFIRMED, source="test", data={})
    await bus.publish(event)
    await bus.publish(event)  # même identifiant : rejoué
    await bus.publish(Event(type="stock.received", source="test", data={}))
    await bus.drain()
    assert got == [ORDER_CONFIRMED]


def test_vendor_a_mapping():
    msg = to_vendor_a(Event(type=ORDER_CONFIRMED, source="t", data=ORDER))
    assert msg["messageType"] == "ORDER_ACK"
    assert msg["items"][0] == {"itemCode": "SW-PON-005", "label": "Poncho, éponge", "unitPrice": 29.0,
                               "ordered": 2, "confirmed": 0, "unscheduled": 0,
                               "backorder": [{"quantity": 2, "expectedDate": "2026-11-09"}]}
    assert sign("s", b"x").startswith("sha256=")


def test_vendor_b_csv_is_rfc4180():
    text = to_vendor_b_csv(Event(type=ORDER_CONFIRMED, source="t", data=ORDER))
    assert "\r\n" in text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == CSV_HEADER
    assert rows[1][3] == "Poncho, éponge"  # virgule correctement échappée
    assert rows[1][6:8] == ["2", "2026-11-09"]

    shipment = {"shipment_id": "BL-1", "order_id": "CMD-1", "shop": SHOP, "dispatched_at": "2026-11-10T08:00:00Z",
                "lines": [{"sku": "SW-PON-005", "name": "Poncho", "qty": 2}]}
    rows = list(csv.reader(io.StringIO(to_vendor_b_csv(Event(type=SHIPMENT_DISPATCHED, source="t", data=shipment)))))
    assert rows[1][0] == "LIVRAISON" and rows[1][-2:] == ["2", "BL-1"]
