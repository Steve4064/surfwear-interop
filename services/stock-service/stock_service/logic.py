"""Règles métier du stock : disponibilité, allocation, reliquats.

Principe anti double-réservation : toute prise de quantité se fait par une
mise à jour conditionnelle atomique (``UPDATE ... SET reserved = reserved + q
WHERE on_hand - reserved >= q``). Deux transactions concurrentes ne peuvent
donc jamais promettre la même pièce : la seconde voit sa mise à jour refusée
et se replie sur le lot suivant ou sur un reliquat.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from surfwear_common.events import (
    AllocationUpdated,
    ShipmentDispatched,
    StockIncoming,
    StockReceived,
)

from .models import Allocation, Lot, Product

MAX_RETRIES = 5


class UnknownSku(Exception):
    def __init__(self, skus: list[str]):
        super().__init__(", ".join(skus))
        self.skus = skus


# --------------------------------------------------------------------------- #
# Primitives atomiques                                                         #
# --------------------------------------------------------------------------- #
async def take_from_stock(s: AsyncSession, sku: str, wanted: int) -> int:
    """Réserve jusqu'à ``wanted`` unités sur le stock réel. Renvoie la quantité prise."""
    for _ in range(MAX_RETRIES):
        row = (
            await s.execute(select(Product.on_hand, Product.reserved).where(Product.sku == sku))
        ).one()
        take = min(wanted, row.on_hand - row.reserved)
        if take <= 0:
            return 0
        res = await s.execute(
            update(Product)
            .where(Product.sku == sku, Product.on_hand - Product.reserved >= take)
            .values(reserved=Product.reserved + take)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            return take
    return 0


async def take_from_lot(s: AsyncSession, lot_id: str, wanted: int) -> int:
    """Planifie jusqu'à ``wanted`` unités sur un lot non encore reçu."""
    for _ in range(MAX_RETRIES):
        row = (
            await s.execute(
                select(Lot.qty, Lot.allocated, Lot.received).where(Lot.id == lot_id)
            )
        ).one()
        if row.received:
            return 0
        take = min(wanted, row.qty - row.allocated)
        if take <= 0:
            return 0
        res = await s.execute(
            update(Lot)
            .where(Lot.id == lot_id, Lot.qty - Lot.allocated >= take, Lot.received.is_(False))
            .values(allocated=Lot.allocated + take)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            return take
    return 0


async def open_lots(s: AsyncSession, sku: str) -> list[Lot]:
    return list(
        (
            await s.execute(
                select(Lot)
                .where(Lot.sku == sku, Lot.received.is_(False))
                .order_by(Lot.eta, Lot.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# Lecture                                                                      #
# --------------------------------------------------------------------------- #
async def catalog(s: AsyncSession) -> list[dict]:
    products = (await s.execute(select(Product).order_by(Product.sku))).scalars().all()
    lots = (
        await s.execute(select(Lot).where(Lot.received.is_(False)).order_by(Lot.eta))
    ).scalars().all()
    upcoming: dict[str, list[dict]] = {}
    for lot in lots:
        free = lot.qty - lot.allocated
        if free > 0:
            upcoming.setdefault(lot.sku, []).append({"eta": lot.eta, "available": free})
    return [
        {
            "sku": p.sku,
            "name": p.name,
            "category": p.category,
            "unit_price": p.unit_price,
            "available_now": p.on_hand - p.reserved,
            "upcoming": upcoming.get(p.sku, []),
        }
        for p in products
    ]


async def availability(s: AsyncSession, sku: str, qty: int) -> dict:
    """Simule l'allocation d'une quantité, sans rien réserver."""
    product = await s.get(Product, sku)
    if product is None:
        raise UnknownSku([sku])
    free_now = product.on_hand - product.reserved
    plan: list[dict] = []
    rest = qty
    now = min(rest, max(free_now, 0))
    if now:
        plan.append({"qty": now, "eta": None, "source": "stock"})
        rest -= now
    for lot in await open_lots(s, sku):
        if rest <= 0:
            break
        t = min(rest, lot.qty - lot.allocated)
        if t > 0:
            plan.append({"qty": t, "eta": lot.eta, "source": "lot"})
            rest -= t
    if rest > 0:
        plan.append({"qty": rest, "eta": None, "source": "unscheduled"})
    etas = [p["eta"] for p in plan if p["source"] == "lot"]
    if rest > 0:
        complete_by = None  # une partie n'a encore aucun lot de production identifié
    elif etas:
        complete_by = max(etas)
    else:
        complete_by = date.today()
    return {
        "sku": sku,
        "requested": qty,
        "available_now": max(free_now, 0),
        "can_ship_now": now == qty,
        "complete_by": complete_by,
        "plan": plan,
    }


# --------------------------------------------------------------------------- #
# Allocation d'une commande                                                    #
# --------------------------------------------------------------------------- #
def _summary(allocs: list[Allocation], products: dict[str, Product]) -> list[dict]:
    lines: "OrderedDict[str, dict]" = OrderedDict()
    for a in allocs:
        p = products[a.sku]
        line = lines.setdefault(
            a.sku,
            {
                "sku": a.sku,
                "name": p.name,
                "unit_price": p.unit_price,
                "requested": 0,
                "reserved": 0,
                "scheduled": [],
                "unscheduled": 0,
            },
        )
        line["requested"] += a.qty
        if a.kind == "stock":
            line["reserved"] += a.qty
        elif a.kind == "lot":
            line["scheduled"].append({"qty": a.qty, "eta": a.eta, "lot_id": a.lot_id})
        else:
            line["unscheduled"] += a.qty
    return list(lines.values())


async def allocate_order(s: AsyncSession, order_id: str, lines: list[tuple[str, int]]) -> list[dict]:
    """Alloue chaque ligne : stock réel, puis lots par date, puis reliquat non planifié.

    Idempotent : rappeler avec le même ``order_id`` renvoie l'allocation existante.
    """
    existing = list(
        (
            await s.execute(select(Allocation).where(Allocation.order_id == order_id).order_by(Allocation.id))
        ).scalars()
    )
    wanted: "OrderedDict[str, int]" = OrderedDict()
    for sku, qty in lines:
        wanted[sku] = wanted.get(sku, 0) + qty

    products = {
        p.sku: p
        for p in (await s.execute(select(Product).where(Product.sku.in_(list(wanted))))).scalars()
    }
    if existing:
        products.update({p.sku: p for p in (await s.execute(select(Product))).scalars()})
        return _summary(existing, products)

    unknown = [sku for sku in wanted if sku not in products]
    if unknown:
        raise UnknownSku(unknown)

    created: list[Allocation] = []
    for sku, qty in wanted.items():
        rest = qty
        got = await take_from_stock(s, sku, rest)
        if got:
            created.append(Allocation(order_id=order_id, sku=sku, qty=got, kind="stock"))
            rest -= got
        if rest:
            for lot in await open_lots(s, sku):
                t = await take_from_lot(s, lot.id, rest)
                if t:
                    created.append(
                        Allocation(order_id=order_id, sku=sku, qty=t, kind="lot", lot_id=lot.id, eta=lot.eta)
                    )
                    rest -= t
                if not rest:
                    break
        if rest:
            created.append(Allocation(order_id=order_id, sku=sku, qty=rest, kind="unscheduled"))
    s.add_all(created)
    await s.flush()
    return _summary(created, products)


# --------------------------------------------------------------------------- #
# Événements entrants                                                          #
# --------------------------------------------------------------------------- #
def _move(s: AsyncSession, a: Allocation, qty: int, **fields) -> Allocation:
    """Déplace ``qty`` unités de l'allocation ``a`` vers une nouvelle source."""
    if qty == a.qty:
        for k, v in fields.items():
            setattr(a, k, v)
        return a
    a.qty -= qty
    moved = Allocation(order_id=a.order_id, sku=a.sku, qty=qty, **fields)
    s.add(moved)
    return moved


async def _unscheduled(s: AsyncSession, sku: str) -> list[Allocation]:
    return list(
        (
            await s.execute(
                select(Allocation)
                .where(Allocation.sku == sku, Allocation.kind == "unscheduled")
                .order_by(Allocation.id)
            )
        ).scalars()
    )


async def on_stock_incoming(s: AsyncSession, data: StockIncoming) -> list[AllocationUpdated]:
    """Un lot est annoncé : il sert d'abord les reliquats non planifiés (FIFO)."""
    if await s.get(Product, data.sku) is None:
        return []
    if await s.get(Lot, data.lot_id) is None:
        s.add(Lot(id=data.lot_id, sku=data.sku, qty=data.qty, eta=data.eta))
        await s.flush()
    else:
        return []  # lot déjà connu (message rejoué)

    out: list[AllocationUpdated] = []
    for a in await _unscheduled(s, data.sku):
        t = await take_from_lot(s, data.lot_id, a.qty)
        if not t:
            break
        _move(s, a, t, kind="lot", lot_id=data.lot_id, eta=data.eta)
        out.append(
            AllocationUpdated(
                order_id=a.order_id, sku=a.sku, qty=t, status="scheduled",
                previous="unscheduled", eta=data.eta, lot_id=data.lot_id,
            )
        )
    await s.flush()
    return out


async def on_stock_received(s: AsyncSession, data: StockReceived) -> list[AllocationUpdated]:
    """Un lot arrive en entrepôt : ses allocations deviennent des réservations fermes."""
    if await s.get(Product, data.sku) is None:
        return []
    lot = await s.get(Lot, data.lot_id)
    if lot is None:  # réception sans annonce préalable
        lot = Lot(id=data.lot_id, sku=data.sku, qty=data.qty, eta=date.today())
        s.add(lot)
        await s.flush()
    res = await s.execute(
        update(Lot)
        .where(Lot.id == data.lot_id, Lot.received.is_(False))
        .values(received=True)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        return []  # déjà reçu

    await s.execute(
        update(Product)
        .where(Product.sku == data.sku)
        .values(on_hand=Product.on_hand + data.qty)
        .execution_options(synchronize_session=False)
    )

    out: list[AllocationUpdated] = []
    capacity = data.qty
    lot_allocs = (
        await s.execute(
            select(Allocation)
            .where(Allocation.lot_id == data.lot_id, Allocation.kind == "lot")
            .order_by(Allocation.id)
        )
    ).scalars().all()
    for a in lot_allocs:
        t = min(a.qty, capacity)
        rest = a.qty - t
        if t:
            await s.execute(
                update(Product)
                .where(Product.sku == data.sku)
                .values(reserved=Product.reserved + t)
                .execution_options(synchronize_session=False)
            )
            capacity -= t
            _move(s, a, t, kind="stock", lot_id=None, eta=None)
            out.append(
                AllocationUpdated(
                    order_id=a.order_id, sku=a.sku, qty=t, status="reserved",
                    previous="lot", lot_id=data.lot_id,
                )
            )
        if rest:  # lot reçu incomplet : le solde redevient un reliquat non planifié
            a.kind, a.lot_id, a.eta = "unscheduled", None, None
            out.append(
                AllocationUpdated(
                    order_id=a.order_id, sku=a.sku, qty=rest, status="unscheduled", previous="lot"
                )
            )
    await s.flush()

    # Le stock libre restant sert les reliquats non planifiés, dans l'ordre d'arrivée
    for a in await _unscheduled(s, data.sku):
        t = await take_from_stock(s, data.sku, a.qty)
        if not t:
            break
        _move(s, a, t, kind="stock", lot_id=None, eta=None)
        out.append(AllocationUpdated(order_id=a.order_id, sku=a.sku, qty=t, status="reserved", previous="unscheduled"))
    await s.flush()
    return out


async def on_shipment(s: AsyncSession, data: ShipmentDispatched) -> None:
    """Les quantités expédiées sortent du stock réel et des réservations."""
    for line in data.lines:
        await s.execute(
            update(Product)
            .where(Product.sku == line.sku)
            .values(on_hand=Product.on_hand - line.qty, reserved=Product.reserved - line.qty)
            .execution_options(synchronize_session=False)
        )
        rest = line.qty
        allocs = (
            await s.execute(
                select(Allocation)
                .where(Allocation.order_id == data.order_id, Allocation.sku == line.sku, Allocation.kind == "stock")
                .order_by(Allocation.id)
            )
        ).scalars().all()
        for a in allocs:
            if rest <= 0:
                break
            t = min(a.qty, rest)
            rest -= t
            if t == a.qty:
                await s.execute(delete(Allocation).where(Allocation.id == a.id))
            else:
                a.qty -= t
    await s.flush()


# --------------------------------------------------------------------------- #
# Libération d'une pré-réservation expirée ou annulée                          #
# --------------------------------------------------------------------------- #
async def rebalance(s: AsyncSession, sku: str) -> list[AllocationUpdated]:
    """Sert les reliquats non planifiés (FIFO) avec le stock libre, puis avec les lots ouverts."""
    out: list[AllocationUpdated] = []
    for a in await _unscheduled(s, sku):
        t = await take_from_stock(s, sku, a.qty)
        if t:
            rest = a.qty - t
            _move(s, a, t, kind="stock", lot_id=None, eta=None)
            out.append(AllocationUpdated(order_id=a.order_id, sku=sku, qty=t, status="reserved", previous="unscheduled"))
            if not rest:
                continue
        for lot in await open_lots(s, sku):
            if a.kind != "unscheduled" or a.qty <= 0:
                break
            t = await take_from_lot(s, lot.id, a.qty)
            if t:
                _move(s, a, t, kind="lot", lot_id=lot.id, eta=lot.eta)
                out.append(AllocationUpdated(order_id=a.order_id, sku=sku, qty=t, status="scheduled",
                                             previous="unscheduled", eta=lot.eta, lot_id=lot.id))
        await s.flush()
    return out


async def release_order(s: AsyncSession, order_id: str) -> dict:
    """Rend au stock tout ce qu'une commande non finalisée avait bloqué.

    Idempotent : une commande déjà libérée n'a plus d'allocation.
    Le stock rendu sert ensuite les reliquats des autres commandes (ordre d'arrivée).
    """
    allocs = (
        await s.execute(select(Allocation).where(Allocation.order_id == order_id).order_by(Allocation.id))
    ).scalars().all()
    released: dict[str, int] = {}
    for a in allocs:
        if a.kind == "stock":
            await s.execute(
                update(Product).where(Product.sku == a.sku)
                .values(reserved=Product.reserved - a.qty)
                .execution_options(synchronize_session=False)
            )
        elif a.kind == "lot" and a.lot_id:
            await s.execute(
                update(Lot).where(Lot.id == a.lot_id)
                .values(allocated=Lot.allocated - a.qty)
                .execution_options(synchronize_session=False)
            )
        released[a.sku] = released.get(a.sku, 0) + a.qty
        await s.execute(delete(Allocation).where(Allocation.id == a.id))
    await s.flush()
    s.expire_all()

    events: list[AllocationUpdated] = []
    for sku in released:
        events += await rebalance(s, sku)
    return {"order_id": order_id, "released": released, "events": events}


# --------------------------------------------------------------------------- #
# Référentiel produits et inventaire                                           #
# --------------------------------------------------------------------------- #
async def upsert_product(s: AsyncSession, data: dict) -> None:
    """Crée (stock à 0) ou met à jour la copie locale d'une fiche produit du Catalogue."""
    p = await s.get(Product, data["sku"])
    if p is None:
        s.add(Product(sku=data["sku"], name=data["name"], category=data.get("category", ""),
                      unit_price=data["unit_price"], on_hand=0, reserved=0))
    else:
        p.name, p.category, p.unit_price = data["name"], data.get("category", p.category), data["unit_price"]
    await s.flush()


async def adjust_stock(s: AsyncSession, sku: str, on_hand: int) -> list[AllocationUpdated]:
    """Inventaire : le stock physique constaté remplace le stock connu.

    Le stock ne descend jamais sous les quantités déjà réservées (elles sont dans les
    colis en préparation) ; un surplus sert les reliquats en attente.
    """
    p = await s.get(Product, sku)
    if p is None:
        return []
    p.on_hand = max(on_hand, p.reserved)
    await s.flush()
    return await rebalance(s, sku)


async def stock_levels(s: AsyncSession) -> list[dict]:
    rows = await catalog(s)
    products = {p.sku: p for p in (await s.execute(select(Product))).scalars()}
    for r in rows:
        r["on_hand"] = products[r["sku"]].on_hand
        r["reserved"] = products[r["sku"]].reserved
    return rows
