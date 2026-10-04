"""Streaming API over secure websockets: wss://<host>/api/v1/stream

Push instead of polling. One connection can subscribe to events and stream endpoint data in chunks.

Authentication (an API key, as for REST):
  * `X-API-Key` header on the websocket handshake (scripts and services), or
  * first message `{"type": "auth", "api_key": "..."}` within 10 seconds (browsers can't set headers).
  Keys are never accepted in the URL, which would end up in proxy logs.

Client messages (JSON text frames, at most 64 KB):
  {"type": "subscribe",   "topics": ["endpoint:customer-orders", "runs"]}
  {"type": "unsubscribe", "topics": [...]}
  {"type": "query", "id": "q1", "endpoint": "customer-orders", "params": {"region": "EMEA"},
   "chunk_size": 1000, "version": null}
  {"type": "cancel", "id": "q1"}
  {"type": "ping"}

Server messages:
  {"type": "welcome", "key": "<key name>", "limits": {...}}
  {"type": "subscribed", "topics": [...], "denied": {"topic": "reason"}}
  {"type": "event", "topic": "endpoint:customer-orders", "event": "dataset.published", "at": ..., "data": {...}}
  {"type": "event", "topic": "runs", "event": "run.finished", ...}
  {"type": "rows", "id": "q1", "seq": 0, "data": [...]}   ... then
  {"type": "end", "id": "q1", "total": 1234, "chunks": 2, "version": 7}
  {"type": "error", "id": "q1"?, "message": "..."}
  {"type": "heartbeat", "at": ...}   every 30 seconds
  {"type": "pong"}

Topics: `endpoint:<slug>` needs a key allowed for that endpoint (or a public endpoint); `runs` needs a key with
access to all endpoints ("*"), like the REST admin routes. The key and every subscription are re-checked every
minute: revoking a key or removing an endpoint from it ends the stream (close code 4401) or the subscription.

Close codes: 4401 not authenticated / key revoked, 4408 no auth message in time, 1008 more than 50 messages
in 10 seconds, 4429 too many connections for
this key, 4400 protocol misuse, 1009 message too large, 1013 consumer too slow (events backed up), 1000 idle.
Transport rules (wss only, origin check, connection limits) are enforced in databridge/web_security.py.
"""

import asyncio
import json
import logging
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from databridge.config import settings
from databridge.services import endpoints as ep_svc
from databridge.services import events
from databridge.web_security import STREAM_WS, audit_throttled

router = APIRouter()
log = logging.getLogger("databridge.stream")

AUTH_TIMEOUT = 10
HEARTBEAT_SECONDS = 30
RECHECK_SECONDS = 60
MAX_TOPICS = 50
MAX_QUERIES = 2
DEFAULT_CHUNK = 1000
MESSAGES_PER_10S = 50
# Streaming queries run on their own small thread pool: at most this many read data at once for all clients,
# and they can't crowd out the threads the REST API and the studio use. Memory is one chunk per query.
QUERY_THREADS = 4
_QUERY_POOL = ThreadPoolExecutor(QUERY_THREADS, thread_name_prefix="stream-query")
_per_key: Counter = Counter()


class Closing(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _topic_denied(topic: str, key) -> str | None:
    """Why this key may not subscribe to `topic`, or None."""
    if topic == "runs":
        return None if "*" in key.endpoints else "needs a key with access to all endpoints"
    if topic.startswith("endpoint:"):
        slug = topic.split(":", 1)[1]
        ep = ep_svc.get_endpoint_by_slug(slug)
        if not ep or not ep.active:
            return "unknown or inactive endpoint"
        if ep.public or "*" in key.endpoints or slug in key.endpoints:
            return None
        return "key not allowed for this endpoint"
    return "unknown topic (use endpoint:<slug> or runs)"


class Connection:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.ip = ws.client.host if ws.client else "?"
        self.raw_key: str | None = None
        self.key = None
        self.sub = events.Subscriber(loop=asyncio.get_running_loop())
        self.send_lock = asyncio.Lock()
        self.queries: dict[str, asyncio.Task] = {}
        self.last_message = time.monotonic()
        self.counted = False  # holds one of this key's connection slots
        self.closed = False
        self.recent_messages: deque = deque(maxlen=MESSAGES_PER_10S)

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self.send_lock:
            try:
                await self.ws.send_text(json.dumps(message, default=str))
            except (RuntimeError, WebSocketDisconnect, OSError):
                self.closed = True

    # ------------------------------------------------------------ auth
    async def authenticate(self) -> None:
        self.raw_key = self.ws.headers.get("x-api-key")
        if not self.raw_key:
            try:
                first = await asyncio.wait_for(self.ws.receive_text(), AUTH_TIMEOUT)
            except asyncio.TimeoutError:
                raise Closing(4408, "authenticate within 10 seconds") from None
            try:
                msg = json.loads(first)
            except ValueError:
                msg = {}
            if not isinstance(msg, dict) or msg.get("type") != "auth" or not isinstance(msg.get("api_key"), str):
                raise Closing(4401, 'first message must be {"type": "auth", "api_key": "..."}')
            self.raw_key = msg["api_key"]
        self.key = await run_in_threadpool(ep_svc.active_key, self.raw_key)
        if not self.key:
            audit_throttled(self.ip, STREAM_WS, "invalid or revoked API key", "security.stream_denied")
            raise Closing(4401, "invalid or revoked API key")
        if _per_key[self.key.id] >= settings.stream_max_per_key:
            raise Closing(4429, f"at most {settings.stream_max_per_key} streaming connections per key")
        _per_key[self.key.id] += 1
        self.counted = True
        audit_throttled(self.ip, STREAM_WS, f"key {self.key.name}", "api.stream_connected",
                        username=f"api-key:{self.key.name}")

    # ------------------------------------------------------------ background tasks
    async def forward_events(self) -> None:
        while True:
            message = await self.sub.queue.get()
            if self.sub.overflowed:
                raise Closing(1013, "consumer too slow: events backed up")
            if message.get("topic") in self.sub.topics:  # may have unsubscribed meanwhile
                await self.send(message)

    async def housekeeping(self) -> None:
        last_check = time.monotonic()
        while True:
            await asyncio.sleep(min(HEARTBEAT_SECONDS, RECHECK_SECONDS))
            if self.sub.overflowed:
                raise Closing(1013, "consumer too slow: events backed up")
            await self.send({"type": "heartbeat", "at": _now()})
            if time.monotonic() - last_check >= RECHECK_SECONDS:
                last_check = time.monotonic()
                await self.recheck()
            idle = time.monotonic() - self.last_message
            if not self.sub.topics and not self.queries and idle > settings.stream_idle_minutes * 60:
                raise Closing(1000, "idle")

    async def recheck(self) -> None:
        key = await run_in_threadpool(ep_svc.active_key, self.raw_key)
        if not key:
            raise Closing(4401, "API key revoked")
        self.key = key
        for topic in sorted(self.sub.topics):
            reason = await run_in_threadpool(_topic_denied, topic, key)
            if reason:
                self.sub.topics.discard(topic)
                await self.send({"type": "unsubscribed", "topic": topic, "reason": reason})

    # ------------------------------------------------------------ client messages
    async def handle(self, text: str) -> None:
        now = time.monotonic()
        self.last_message = now
        if len(self.recent_messages) == MESSAGES_PER_10S and now - self.recent_messages[0] < 10:
            raise Closing(1008, f"too many messages (at most {MESSAGES_PER_10S} per 10 seconds)")
        self.recent_messages.append(now)
        try:
            msg = json.loads(text)
        except ValueError:
            return await self.send({"type": "error", "message": "messages must be JSON"})
        if not isinstance(msg, dict):
            return await self.send({"type": "error", "message": "messages must be JSON objects"})
        kind = msg.get("type")
        if kind == "ping":
            return await self.send({"type": "pong", "at": _now()})
        if kind in ("subscribe", "unsubscribe"):
            return await self.subscribe(msg.get("topics"), kind == "subscribe")
        if kind == "query":
            return await self.start_query(msg)
        if kind == "cancel":
            task = self.queries.get(str(msg.get("id")))
            if task:
                task.cancel()
            return None
        if kind == "auth":
            return await self.send({"type": "error", "message": "already authenticated"})
        return await self.send({"type": "error", "message": f"unknown message type {kind!r}"})

    async def subscribe(self, topics, add: bool) -> None:
        if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
            return await self.send({"type": "error", "message": "topics must be a list of strings"})
        if not add:
            for t in topics:
                self.sub.topics.discard(t)
            return await self.send({"type": "unsubscribed", "topics": topics})
        granted, denied = [], {}
        for t in topics[:MAX_TOPICS]:
            reason = await run_in_threadpool(_topic_denied, t, self.key)
            if reason:
                denied[t] = reason
            elif len(self.sub.topics) >= MAX_TOPICS:
                denied[t] = f"at most {MAX_TOPICS} topics per connection"
            else:
                self.sub.topics.add(t)
                granted.append(t)
        await self.send({"type": "subscribed", "topics": granted, "denied": denied})

    async def start_query(self, msg: dict) -> None:
        qid = msg.get("id")
        if not isinstance(qid, str) or not 0 < len(qid) <= 64:
            return await self.send({"type": "error", "message": "query needs an id (string, up to 64 characters)"})
        if qid in self.queries:
            return await self.send({"type": "error", "id": qid, "message": "a query with this id is running"})
        if len(self.queries) >= MAX_QUERIES:
            return await self.send({"type": "error", "id": qid,
                                    "message": f"at most {MAX_QUERIES} queries at a time per connection"})
        task = asyncio.create_task(self.run_query(qid, msg))
        self.queries[qid] = task

        def finished(t: asyncio.Task, q=qid) -> None:
            self.queries.pop(q, None)
            if t.cancelled() and not self.closed:  # cancelled before it started: run_query never answered
                asyncio.get_running_loop().create_task(self.send({"type": "cancelled", "id": q}))

        task.add_done_callback(finished)

    async def run_query(self, qid: str, msg: dict) -> None:
        bq = None
        current = None  # the worker-thread call in progress

        async def call(fn, *args):
            nonlocal current
            current = _QUERY_POOL.submit(fn, *args)
            return await asyncio.wrap_future(current)

        try:
            slug = msg.get("endpoint")
            params = msg.get("params") or {}
            if not isinstance(slug, str) or not isinstance(params, dict) or len(params) > 50:
                raise ep_svc.EndpointError(400, "query needs endpoint (string) and params (object)")
            chunk = max(1, min(int(msg.get("chunk_size") or DEFAULT_CHUNK), settings.max_page_size))
            version = msg.get("version")
            version = int(version) if version not in (None, "") else None
            if version is not None and not 0 < version < 2**31:
                raise ValueError("version out of range")
            ep = await run_in_threadpool(ep_svc.get_endpoint_by_slug, slug)
            if not ep:
                raise ep_svc.EndpointError(404, f"Endpoint {slug[:80]!r} not found")
            await run_in_threadpool(ep_svc.authorize, ep, self.raw_key)
            args = {str(k)[:100]: ("" if v is None else str(v)[:1000]) for k, v in params.items()}
            bq = await call(ep_svc.BatchQuery, ep, args, version)
            total = await call(bq.count)
            await call(bq.start)
            seq = 0
            while not self.closed:
                rows = await call(bq.next_rows, chunk)
                if not rows:
                    break
                await self.send({"type": "rows", "id": qid, "seq": seq, "data": rows})
                seq += 1
            await self.send({"type": "end", "id": qid, "total": total, "chunks": seq, "version": bq.version})
        except asyncio.CancelledError:
            await self.send({"type": "cancelled", "id": qid})
        except ep_svc.EndpointError as e:
            await self.send({"type": "error", "id": qid, "status": e.status, "message": str(e)})
        except (TypeError, ValueError, OverflowError):
            await self.send({"type": "error", "id": qid, "status": 400,
                             "message": "bad query: check chunk_size, version and params"})
        except Exception:  # never leave the client waiting, never send internals
            log.exception("streaming query %s failed", qid)
            await self.send({"type": "error", "id": qid, "status": 500, "message": "query failed"})
        finally:
            self._release(bq, current)

    @staticmethod
    def _release(bq, current) -> None:
        """Stops the query's database work and closes it once its worker thread is done (never under it)."""
        if bq is not None:
            bq.interrupt()
        if current is not None and not current.done():
            current.cancel()  # not started yet: never runs

            def close_when_done(f, b=bq):
                target = b
                if target is None and not f.cancelled() and f.exception() is None:
                    target = f.result()  # the query was still being opened
                if isinstance(target, ep_svc.BatchQuery):
                    target.interrupt()
                    target.close()

            current.add_done_callback(close_when_done)
        elif bq is not None:
            bq.close()

    async def receive_loop(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("text") is None:
                raise Closing(4400, "send JSON text frames, not binary")
            await self.handle(message["text"])


@router.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    conn = Connection(ws)
    tasks: list[asyncio.Task] = []
    try:
        await conn.authenticate()
        events.subscribe(conn.sub)
        await conn.send({"type": "welcome", "key": conn.key.name, "limits": {
            "max_topics": MAX_TOPICS, "max_queries": MAX_QUERIES, "max_message_bytes": 64 * 1024,
            "max_messages_per_10s": MESSAGES_PER_10S,
            "heartbeat_seconds": HEARTBEAT_SECONDS, "max_chunk_size": settings.max_page_size}})
        tasks = [asyncio.create_task(t) for t in (conn.receive_loop(), conn.forward_events(), conn.housekeeping())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            t.result()  # re-raise the reason the connection ends
    except Closing as c:
        try:
            await ws.close(code=c.code, reason=c.reason[:120])
        except RuntimeError:
            pass
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("streaming connection failed")
        try:
            await ws.close(code=1011, reason="server error")
        except RuntimeError:
            pass
    finally:
        conn.closed = True
        for t in [*tasks, *conn.queries.values()]:
            t.cancel()
        events.unsubscribe(conn.sub)
        if conn.counted:
            _per_key[conn.key.id] -= 1
            if not _per_key[conn.key.id]:
                del _per_key[conn.key.id]
