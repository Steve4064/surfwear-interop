"""Service Notifications : remplace les fiches envoyées par mail par le commercial."""
from __future__ import annotations

import asyncio
import smtplib
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from email.message import EmailMessage

from fastapi import FastAPI

from surfwear_common.bus import EventBus, bus_from_url
from surfwear_common.events import ORDER_CONFIRMED, ORDER_UPDATED, SHIPMENT_DISPATCHED, Event
from surfwear_common.service import env, setup_logging

SERVICE = "notification-service"
log = setup_logging(SERVICE)


def fr_date(iso: str | None) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y") if iso else "date à confirmer"


def order_lines(d: dict) -> list[str]:
    out = []
    for l in d["lines"]:
        parts = [f"{l['qty_reserved']} réservé(s)"]
        parts += [f"{s['qty']} livrable(s) vers le {fr_date(s['eta'])}" for s in l["scheduled"]]
        if l["qty_unscheduled"]:
            parts.append(f"{l['qty_unscheduled']} en attente de production")
        if l["qty_shipped"]:
            parts.append(f"{l['qty_shipped']} expédié(s)")
        out.append(f"- {l['name']} ({l['sku']}) : {l['qty_ordered']} commandé(s) -> " + ", ".join(parts))
    return out


def render(event: Event) -> tuple[str, str]:
    d = event.data
    shop = d["shop"]["name"]
    if event.type == ORDER_CONFIRMED:
        subject = f"Commande {d['order_id']} finalisée"
        intro = f"Bonjour {shop},\n\nVotre commande {d['order_id']} est enregistrée ({d['total_amount']:.2f} € HT)."
        return subject, "\n".join([intro, "", *order_lines(d), "", "Les quantités réservées partent à la prochaine expédition."])
    if event.type == ORDER_UPDATED:
        c = d.get("change") or {}
        what = {
            "reserved": "est désormais réservé et partira à la prochaine expédition",
            "scheduled": f"est planifié sur un lot de production, livrable vers le {fr_date(c.get('eta'))}",
            "unscheduled": "attend un nouveau lot de production",
        }.get(c.get("status"), "a changé")
        subject = f"Commande {d['order_id']} : mise à jour du reliquat"
        body = f"Bonjour {shop},\n\nUn reliquat de {c.get('qty')} x {c.get('sku')} {what}.\n\nÉtat de la commande :\n"
        return subject, body + "\n".join(order_lines(d))
    subject = f"Commande {d['order_id']} expédiée, bon de livraison {d['shipment_id']}"
    lines = [f"- {l['name']} ({l['sku']}) : {l['qty']}" for l in d["lines"]]
    return subject, "\n".join([f"Bonjour {shop},", "", f"Votre colis est parti (bon de livraison {d['shipment_id']}) :", *lines])


def create_app(bus: EventBus | None = None, smtp_host: str | None = None) -> FastAPI:
    bus = bus or bus_from_url(env("BUS_URL", "memory://"))
    smtp_host = env("SMTP_HOST", "") if smtp_host is None else smtp_host
    smtp_port = int(env("SMTP_PORT", "1025"))
    sender = env("MAIL_FROM", "commandes@surfwear.example")
    sent: deque = deque(maxlen=200)

    def send(to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.send_message(msg)

    async def on_event(event: Event) -> None:
        subject, body = render(event)
        to = event.data["shop"]["email"]
        if smtp_host:
            await asyncio.to_thread(send, to, subject, body)
        sent.appendleft({"to": to, "subject": subject, "body": body, "event": event.type})
        log.info("Notification « %s » -> %s", subject, to)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.subscribe(f"{SERVICE}.all", [ORDER_CONFIRMED, ORDER_UPDATED, SHIPMENT_DISPATCHED], on_event)
        await bus.connect()
        yield
        await bus.close()

    app = FastAPI(root_path=env("ROOT_PATH", ""), title="Service Notifications", version="1.0.0", lifespan=lifespan)
    app.state.sent = sent

    @app.get("/health", tags=["supervision"])
    async def health():
        return {"status": "ok", "service": SERVICE}

    @app.get("/notifications", tags=["supervision"], summary="Dernières notifications envoyées")
    async def notifications():
        return list(sent)

    return app
