"""In-process event bus for the streaming API (single app process, see docker-compose / systemd notes).

Services call `publish(topic, payload)` from any thread; each websocket subscriber gets the event on its own
asyncio queue. A subscriber that falls too far behind is marked `overflowed` and disconnected by its handler,
so one slow consumer can't hold memory or slow the others.

Topics:
    endpoint:<slug>   a new dataset version was published for that endpoint
    runs              an ingest / extract / publish run started or finished
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("databridge.events")
QUEUE_SIZE = 1000


@dataclass(eq=False)
class Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    topics: set[str] = field(default_factory=set)
    overflowed: bool = False


_subs: set[Subscriber] = set()
_lock = threading.Lock()


def subscribe(sub: Subscriber) -> Subscriber:
    with _lock:
        _subs.add(sub)
    return sub


def unsubscribe(sub: Subscriber) -> None:
    with _lock:
        _subs.discard(sub)


def subscriber_count() -> int:
    with _lock:
        return len(_subs)


def _offer(sub: Subscriber, message: dict[str, Any]) -> None:
    try:
        sub.queue.put_nowait(message)
    except asyncio.QueueFull:
        sub.overflowed = True


def _jsonable(value):
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def publish(topic: str, event: str, data: dict[str, Any]) -> int:
    """Delivers {"type": "event", "topic", "event", "at", "data"} to every subscriber of `topic`."""
    message = {"type": "event", "topic": topic, "event": event,
               "at": datetime.now(timezone.utc).isoformat(), "data": _jsonable(data)}
    with _lock:
        targets = [s for s in _subs if topic in s.topics]
    for sub in targets:
        try:
            sub.loop.call_soon_threadsafe(_offer, sub, message)
        except RuntimeError:  # loop already closed: the connection is going away
            pass
    return len(targets)


def publish_safely(topic: str, event: str, data: dict[str, Any]) -> None:
    """For hooks inside services: an event problem must never fail a publish or a run."""
    try:
        publish(topic, event, data)
    except Exception:
        log.exception("event %s on %s not delivered", event, topic)
