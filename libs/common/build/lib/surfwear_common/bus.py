"""Bus d'événements.

Deux implémentations au même contrat :

* ``RabbitBus`` : RabbitMQ (AMQP 0-9-1), échange *topic* durable, une file
  durable par service consommateur, messages persistants, acquittement manuel,
  file de lettres mortes pour les messages en échec.
* ``MemoryBus`` : bus en mémoire pour les tests et le développement sans
  infrastructure. Même sémantique de routage (``*`` = un mot, ``#`` = zéro ou
  plusieurs mots).

Chaque abonnement est protégé par un filtre d'idempotence : un message dont
l'identifiant a déjà été traité par cette file est ignoré.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Awaitable, Callable, Protocol

from .events import DEAD_LETTER_EXCHANGE, EXCHANGE, Event

log = logging.getLogger("surfwear.bus")

Handler = Callable[[Event], Awaitable[None]]


def topic_matches(pattern: str, key: str) -> bool:
    """Correspondance de clé de routage façon AMQP topic."""
    p, k = pattern.split("."), key.split(".")

    def match(i: int, j: int) -> bool:
        if i == len(p):
            return j == len(k)
        if p[i] == "#":
            return any(match(i + 1, jj) for jj in range(j, len(k) + 1))
        if j == len(k):
            return False
        return (p[i] == "*" or p[i] == k[j]) and match(i + 1, j + 1)

    return match(0, 0)


class _SeenIds:
    """Mémoire bornée des identifiants de messages déjà traités."""

    def __init__(self, size: int = 10_000):
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._size = size

    def check_and_add(self, event_id: str) -> bool:
        if event_id in self._ids:
            return False
        self._ids[event_id] = None
        if len(self._ids) > self._size:
            self._ids.popitem(last=False)
        return True


def _idempotent(queue: str, handler: Handler) -> Handler:
    seen = _SeenIds()

    async def wrapped(event: Event) -> None:
        if not seen.check_and_add(event.id):
            log.info("[%s] message %s déjà traité, ignoré", queue, event.id)
            return
        await handler(event)

    return wrapped


class EventBus(Protocol):
    def subscribe(self, queue: str, patterns: list[str], handler: Handler) -> None: ...
    async def connect(self) -> None: ...
    async def publish(self, event: Event) -> None: ...
    async def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Bus en mémoire                                                               #
# --------------------------------------------------------------------------- #
class MemoryBus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, list[str], Handler]] = []
        self._tasks: set[asyncio.Task] = set()
        self.published: list[Event] = []
        self.failures: list[tuple[str, Event, Exception]] = []

    def subscribe(self, queue: str, patterns: list[str], handler: Handler) -> None:
        self._subs.append((queue, patterns, _idempotent(queue, handler)))

    async def connect(self) -> None:  # rien à faire
        return None

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        for queue, patterns, handler in self._subs:
            if any(topic_matches(p, event.type) for p in patterns):
                task = asyncio.create_task(self._deliver(queue, handler, event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _deliver(self, queue: str, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception as exc:  # équivalent d'une lettre morte
            log.exception("[%s] échec du traitement de %s", queue, event.type)
            self.failures.append((queue, event, exc))

    async def drain(self) -> None:
        """Attend que tous les messages (et ceux qu'ils déclenchent) soient traités."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# Bus RabbitMQ                                                                 #
# --------------------------------------------------------------------------- #
class RabbitBus:
    def __init__(self, url: str, prefetch: int = 10) -> None:
        self._url = url
        self._prefetch = prefetch
        self._subs: list[tuple[str, list[str], Handler]] = []
        self._connection = None
        self._channel = None
        self._exchange = None

    def subscribe(self, queue: str, patterns: list[str], handler: Handler) -> None:
        self._subs.append((queue, patterns, _idempotent(queue, handler)))

    async def connect(self, retries: int = 60, delay: float = 2.0) -> None:
        import aio_pika

        for attempt in range(1, retries + 1):
            try:
                self._connection = await aio_pika.connect_robust(self._url)
                break
            except Exception as exc:  # RabbitMQ pas encore prêt
                log.warning("RabbitMQ indisponible (%s/%s) : %s", attempt, retries, exc)
                await asyncio.sleep(delay)
        else:
            raise RuntimeError("Impossible de se connecter à RabbitMQ")

        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )
        dlx = await self._channel.declare_exchange(
            DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
        )
        dead = await self._channel.declare_queue("surfwear.dead-letters", durable=True)
        await dead.bind(dlx)

        for queue, patterns, handler in self._subs:
            await self._bind(queue, patterns, handler)
        log.info("Connecté à RabbitMQ, %s abonnement(s)", len(self._subs))

    async def _bind(self, queue_name: str, patterns: list[str], handler: Handler) -> None:
        queue = await self._channel.declare_queue(
            queue_name,
            durable=True,
            arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE},
        )
        for pattern in patterns:
            await queue.bind(self._exchange, routing_key=pattern)

        async def on_message(message) -> None:
            # requeue=False : un message en échec part en lettre morte
            async with message.process(requeue=False):
                event = Event.model_validate_json(message.body)
                await handler(event)

        await queue.consume(on_message)

    async def publish(self, event: Event) -> None:
        import aio_pika

        if self._exchange is None:
            raise RuntimeError("Bus non connecté")
        await self._exchange.publish(
            aio_pika.Message(
                body=event.model_dump_json().encode(),
                content_type="application/json",
                message_id=event.id,
                type=event.type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=event.type,
        )

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()


def bus_from_url(url: str) -> EventBus:
    if url.startswith("memory"):
        return MemoryBus()
    return RabbitBus(url)
