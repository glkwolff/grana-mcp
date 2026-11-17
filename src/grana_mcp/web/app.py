"""Starlette app for the chat page. Every API call needs ``Authorization: Bearer``."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from grana_mcp import config, memory
from grana_mcp.kitchen import geo, places, prices, receipt
from grana_mcp.kitchen.service import KitchenTools
from grana_mcp.kitchen.store import KitchenError
from grana_mcp.notify import push
from grana_mcp.notify import service as notify
from grana_mcp.server import PRICES_ENV, GranaTools
from grana_mcp.store import db
from grana_mcp.sync import today_sp
from grana_mcp.web import auth, history, usage
from grana_mcp.web.claude_runner import RunnerConfig, estimate_photo, prepare, run_turn
from grana_mcp.web.limits import AuthGuard, ChatLimits

log = logging.getLogger(__name__)
MAX_MESSAGE = 2000
MAX_IMAGE = 1024 * 1024  # the phone sends ~768 px JPEGs (~60-150 kB)
MAX_PHOTO_BODY = MAX_IMAGE * 4 // 3 + 16 * 1024
_DATA_URL = re.compile(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})")
_MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG",), "image/webp": (b"RIFF",)}
STATIC_DIR = Path(__file__).parent / "static"  # built by web-ui/ (npm run build)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        # Menor Preço: the phone looks prices up itself (see web-ui/src/phonePrices.js).
        # blob: photos are opened from a local blob URL to be shrunk (or QR-read) on the phone;
        # wasm-unsafe-eval: the QR reader (ZXing) is WebAssembly served by this app.
        "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
        "connect-src 'self' https://menorpreco.notaparana.pr.gov.br; "
        "img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Session:
    """Per user: one answer at a time. Threads themselves live in the database."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Household snapshot, fixed for the whole conversation so the prompt prefix
    # stays identical turn after turn (Claude's prompt cache).
    context: str | None = None
    thread: str | None = None  # the thread ``context`` belongs to


@dataclass
class State:
    runner: RunnerConfig
    limits: ChatLimits = field(default_factory=ChatLimits)
    guard: AuthGuard = field(default_factory=AuthGuard)
    sessions: dict[str, Session] = field(default_factory=dict)
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)  # thread -> turn
    prices: prices.Fetcher | None = None
    shops: places.Fetcher | None = places.overpass
    receipts: receipt.Fetcher | None = None  # None: the real tax-office page
    receipt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def session(self, user: str) -> Session:
        return self.sessions.setdefault(user, Session())


class Denied(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


def _too_many(message: str, wait: int) -> JSONResponse:
    return JSONResponse(
        {"error": message, "retry_after": wait},
        status_code=429,
        headers={**SECURITY_HEADERS, "Retry-After": str(wait)},
    )


async def _user(request: Request, state: State) -> str:
    """The authenticated user, or raises ``Denied`` (401, or 429 when the IP is blocked)."""
    ip = request.client.host if request.client else "?"
    wait = state.guard.blocked(ip)
    if wait:
        raise Denied(_too_many("muitas tentativas com token errado; espere", wait))
    if ip in auth.trusted_proxies():
        # From the PC itself: through tailscale serve (it names the person in a
        # header and sends their tailnet IP in X-Forwarded-For) or a browser on
        # the PC (no proxy headers: the PC's owner, via whois of its own IP).
        via_proxy = auth.serve_user(ip, request.headers.get(auth.LOGIN_HEADER))
        if via_proxy is not None:
            return via_proxy
        peer = auth.forwarded_peer(request.headers.get("x-forwarded-for"))
        ts_user = await auth.tailscale_user(peer or ip)
    else:
        ts_user = await auth.tailscale_user(ip)
    if ts_user is not None:
        return ts_user
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    if token in ("", "null", "undefined"):
        token = None
    user = auth.verify(token)
    if user is None:
        state.guard.failed(ip)
        await asyncio.sleep(0.5)  # slow down guessing (tokens are 256-bit anyway)
        raise Denied(
            JSONResponse({"error": "não autorizado"}, status_code=401, headers=SECURITY_HEADERS)
        )
    state.guard.succeeded(ip)
    return user


def create_app(
    runner: RunnerConfig | None = None,
    limits: ChatLimits | None = None,
    price_source: prices.Fetcher | None = None,
    shops: places.Fetcher | None = None,
    receipts: receipt.Fetcher | None = None,
) -> Starlette:
    state = State(runner or RunnerConfig(), limits or ChatLimits())
    state.receipts = receipts
    if shops is not None:
        state.shops = shops
    if price_source is not None:
        state.prices = price_source
    elif os.environ.get(PRICES_ENV) == "on":  # off by default: the source blocks automation
        state.prices = prices.MenorPreco()
    prepare(state.runner)

    async def me(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        return JSONResponse({"user": user}, headers=SECURITY_HEADERS)

    async def board_view(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        try:
            data = GranaTools(config.db_path()).board()
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503, headers=SECURITY_HEADERS)
        try:  # the day's calories on the home screen (only when the diet is set up)
            food = await asyncio.to_thread(kitchen(user).food_diary)
            data["food_today"] = {k: food[k] for k in ("intake", "streaks", "week_score")}
        except (KitchenError, sqlite3.Error):
            pass
        return JSONResponse(data, headers=SECURITY_HEADERS)

    async def read_view(request: Request, fn: Callable[[GranaTools], dict[str, Any]]) -> Response:
        try:
            await _user(request, state)
        except Denied as d:
            return d.response
        try:
            data = fn(GranaTools(config.db_path()))
        except ToolError as exc:
            code = 503 if not config.db_path().exists() else 400
            return JSONResponse({"error": str(exc)}, status_code=code, headers=SECURITY_HEADERS)
        return JSONResponse(data, headers=SECURITY_HEADERS)

    def _period_arg(request: Request) -> str:
        return request.query_params.get("period", "this_month")[:32]

    async def insights_view(request: Request) -> Response:
        period = _period_arg(request)
        return await read_view(request, lambda t: t.insights(period))

    async def category_view(request: Request) -> Response:
        period = _period_arg(request)
        name = request.query_params.get("name", "")[:64]
        return await read_view(request, lambda t: t.category_detail(name, period))

    async def push_key(request: Request) -> Response:
        try:
            await _user(request, state)
        except Denied as d:
            return d.response
        return JSONResponse({"key": push.public_key()}, headers=SECURITY_HEADERS)

    async def push_change(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        try:
            body = await request.json()
            endpoint = str(body["endpoint"])
            keys = body.get("keys") or {}
        except (ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse(
                {"error": "pedido inválido"}, status_code=400, headers=SECURITY_HEADERS
            )
        conn = db.connect(config.db_path())
        try:
            if request.url.path.endswith("/unsubscribe"):
                notify.unsubscribe(conn, user, endpoint)
            else:
                notify.subscribe(
                    conn, user, endpoint, str(keys.get("p256dh", "")), str(keys.get("auth", ""))
                )
        except push.PushError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=SECURITY_HEADERS)
        finally:
            conn.close()
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    async def memory_view(request: Request) -> Response:
        """GET: list notes; POST {text}: add; POST /delete {id}: remove."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        body: dict[str, Any] = {}
        if request.method == "POST":
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return JSONResponse(
                    {"error": "pedido inválido"}, status_code=400, headers=SECURITY_HEADERS
                )
        conn = db.connect(config.db_path())
        try:
            if request.url.path.endswith("/delete"):
                try:
                    memory.delete_note(conn, int(body.get("id", 0)))
                except (TypeError, ValueError):
                    return JSONResponse(
                        {"error": "id inválido"}, status_code=400, headers=SECURITY_HEADERS
                    )
            elif request.method == "POST":
                try:
                    memory.add_note(conn, str(body.get("text", "")), user)
                except memory.MemoryError_ as exc:
                    return JSONResponse(
                        {"error": str(exc)}, status_code=400, headers=SECURITY_HEADERS
                    )
            notes = [
                {
                    "id": n["id"],
                    "text": n["text"],
                    "by": n["created_by"],
                    "at": n["created_at"][:10],
                }
                for n in memory.list_notes(conn)
            ]
        finally:
            conn.close()
        return JSONResponse({"notes": notes, "max": memory.MAX_NOTES}, headers=SECURITY_HEADERS)

    def kitchen(user: str) -> KitchenTools:
        if state.receipts is None:
            return KitchenTools(config.db_path(), today_sp, lambda: user, state.prices, state.shops)
        return KitchenTools(
            config.db_path(), today_sp, lambda: user, state.prices, state.shops, state.receipts
        )

    async def phone_prices(request: Request) -> Response:
        """The phone looks prices up itself (its own connection, a few requests at a human
        pace): GET what to look up today, POST each item's raw results."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        k = kitchen(user)
        try:
            if request.method == "GET":
                week = request.query_params.get("week") or None
                data = await asyncio.to_thread(k.price_jobs, week)
            else:
                if int(request.headers.get("content-length") or 0) > MAX_PRICE_BODY:
                    raise ValueError("corpo grande demais")
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("corpo inválido")
                if request.url.path.endswith("/done"):
                    data = await asyncio.to_thread(k.log_lookup, body)
                else:
                    data = await asyncio.to_thread(
                        k.store_phone_prices,
                        body.get("term"),
                        body.get("category"),
                        body.get("items"),
                    )
        except (KitchenError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=SECURITY_HEADERS)
        return JSONResponse(data, headers=SECURITY_HEADERS)

    async def food_photo(request: Request) -> Response:
        """A plate photo -> a cheap one-shot estimate (never in the chat session) -> the
        food diary. The photo is not stored anywhere."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response

        def bad(msg: str, code: int = 400) -> Response:
            return JSONResponse({"error": msg}, status_code=code, headers=SECURITY_HEADERS)

        if int(request.headers.get("content-length") or 0) > MAX_PHOTO_BODY:
            return bad("foto grande demais", 413)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("corpo inválido")
            image = photo(body.get("image"))
            if image is None:
                raise ValueError("mande a foto")
            note = str(body.get("note") or "").strip()[:200] or None
            meal = str(body.get("meal") or "") or meal_now()
        except ValueError as exc:
            return bad(str(exc))
        wait = state.limits.take(user)
        if wait:
            return _too_many(f"limite de pedidos atingido; tente em {wait // 60 + 1} min", wait)
        k = kitchen(user)
        sender, household = await asyncio.to_thread(k.who_is_home)
        est, event = await estimate_photo(state.runner, *image, note, sender, household)
        _record_usage(user, event, 0)
        if est is None:
            return bad(str(event.get("message")), 502)
        try:
            out = await asyncio.to_thread(k.log_photo, est, meal)
        except KitchenError as exc:
            return bad(str(exc))
        return JSONResponse(out, headers=SECURITY_HEADERS)

    async def read_receipt(request: Request) -> Response:
        """The QR code (or link) of a receipt -> its prices. No model call."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        try:
            if int(request.headers.get("content-length") or 0) > MAX_PRICE_BODY:
                raise ValueError("corpo grande demais")
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("corpo inválido")
            async with state.receipt_lock:  # one at a time to the tax office
                data = await asyncio.to_thread(kitchen(user).read_receipt, body.get("qr"))
        except (KitchenError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=SECURITY_HEADERS)
        return JSONResponse(data, headers=SECURITY_HEADERS)

    async def kitchen_view(request: Request) -> Response:
        """GET: the Cozinha tab. POST /api/kitchen/<action>: small writes from the page."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        k = kitchen(user)
        action = request.path_params.get("action")
        try:
            if request.method == "POST":
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("corpo inválido")
                if action == "day":
                    k.log_day(bool(body.get("followed")))
                elif action == "weight":
                    k.log_weight(str(body.get("person", "")), float(body.get("kg", 0)))
                elif action == "location":
                    gh = geo.geohash(float(body["lat"]), float(body["lon"]))
                    k.set_kitchen_rules(home_geohash=gh)
                elif action == "market":
                    k.set_market(str(body.get("code", "")), bool(body.get("favorite", True)))
                elif action == "meal":
                    k.log_planned(str(body.get("meal", "")))
                elif action == "meal_delete":
                    k.delete_meal(int(body["id"]))
                elif action == "discover":
                    await asyncio.to_thread(k.markets, True, True)  # the button means "again"
                elif action == "prices":
                    await asyncio.to_thread(k.shopping_list, None, True)
                else:
                    return JSONResponse(
                        {"error": "ação desconhecida"}, status_code=404, headers=SECURITY_HEADERS
                    )
            data = await asyncio.to_thread(k.page)
            data["markets"] = (await asyncio.to_thread(k.markets))["markets"]
        except (KitchenError, ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400, headers=SECURITY_HEADERS)
        return JSONResponse(data, headers=SECURITY_HEADERS)

    async def new_chat(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        session = state.session(user)
        session.context = session.thread = None
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    def _history_db() -> sqlite3.Connection | None:
        path = config.db_path()
        return db.connect(path) if path.exists() else None

    async def chats(request: Request) -> Response:
        """GET /api/chats: my threads. GET /api/chats/<id>: its messages.
        POST /api/chats/<id>/delete: delete it."""
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        conn = _history_db()
        if conn is None:
            return JSONResponse({"threads": []}, headers=SECURITY_HEADERS)
        try:
            tid = request.path_params.get("tid")
            if tid is None:
                return JSONResponse(
                    {"threads": history.threads(conn, user)}, headers=SECURITY_HEADERS
                )
            row = history.get(conn, user, tid)
            if row is None:
                return JSONResponse(
                    {"error": "conversa não encontrada"}, status_code=404, headers=SECURITY_HEADERS
                )
            if request.method == "POST":
                history.delete(conn, user, tid)
                session = state.session(user)
                if session.thread == tid:
                    session.context = session.thread = None
                return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)
            data = {
                "id": tid,
                "title": row["title"],
                "messages": history.messages(conn, tid),
                "running": tid in state.running,  # an answer is still being written
            }
            return JSONResponse(data, headers=SECURITY_HEADERS)
        finally:
            conn.close()

    async def chat(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        try:
            body = await request.json()
            message = str(body.get("message", "")).strip()
            wanted = body.get("thread")
        except (ValueError, AttributeError):
            message, wanted = "", None
        if not message or len(message) > MAX_MESSAGE:
            return JSONResponse(
                {"error": f"mensagem vazia ou longa demais (máx. {MAX_MESSAGE})"},
                status_code=400,
                headers=SECURITY_HEADERS,
            )
        session = state.session(user)
        if session.lock.locked():
            return JSONResponse(
                {"error": "espere a resposta anterior terminar"},
                status_code=409,
                headers=SECURITY_HEADERS,
            )
        conn = _history_db()
        resume = False
        if isinstance(wanted, str) and wanted:
            row = history.get(conn, user, wanted) if conn is not None else None
            if row is None:
                if conn is not None:
                    conn.close()
                return JSONResponse(
                    {"error": "conversa não encontrada; comece uma nova"},
                    status_code=404,
                    headers=SECURITY_HEADERS,
                )
            tid, resume = wanted, bool(row["started"])
        else:
            tid = history.create(conn, user, message) if conn is not None else str(uuid.uuid4())
        wait = state.limits.take(user)
        if wait:
            if conn is not None:
                conn.close()
            return _too_many(
                f"limite de perguntas atingido ({state.limits.per_hour}/hora, "
                f"{state.limits.per_day}/dia); tente de novo em {wait // 60 + 1} min",
                wait,
            )

        # The turn runs as its own task: if the phone drops the connection (iOS does when
        # the app loses focus) the answer still finishes and lands in the history, where
        # the page picks it up. The stream below only relays events while it is connected.
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await session.lock.acquire()

        async def worker() -> None:
            text, tools, saved, started, asked = "", 0, False, resume, False
            try:
                if session.thread != tid or session.context is None:
                    session.thread = tid
                    session.context = await asyncio.to_thread(_snapshot)
                if conn is not None:
                    history.add(conn, tid, "me", message)
                    asked = True
                turn = run_turn(state.runner, message, user, tid, resume, session.context)
                async for event in turn:
                    kind = event["type"]
                    if not started and (kind in ("text", "tool") or "usage" in event):
                        started = True  # Claude Code created the session: resume from now on
                        if conn is not None:
                            history.mark_started(conn, tid)
                    if kind == "text":
                        text += event["text"]
                    elif kind == "tool":
                        tools += 1
                        text += "\n\n" if text else ""
                    elif kind in ("done", "error"):
                        _record_usage(user, event, tools)
                        if conn is not None:
                            meta = {"usage": event.get("usage"), "tools": tools}
                            answer = event.get("result") or text
                            error = event.get("message") if kind == "error" else None
                            history.add(conn, tid, "bot", answer, meta, error)
                        saved = True
                    queue.put_nowait(event)
            except Exception:  # never leave the user's lock taken
                log.exception("chat turn failed")
                queue.put_nowait({"type": "error", "message": "O assistente teve um erro interno."})
            finally:
                if conn is not None:
                    if asked and not saved:
                        history.add(conn, tid, "bot", text, None, "resposta interrompida")
                    conn.close()
                state.running.pop(tid, None)
                session.lock.release()
                queue.put_nowait(None)

        state.running[tid] = asyncio.create_task(worker())

        async def stream() -> AsyncIterator[bytes]:
            yield _sse({"type": "thread", "id": tid, "title": history.title_of(message)})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
                except TimeoutError:
                    yield HEARTBEAT
                    continue
                if event is None:
                    return
                yield _sse(event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={**SECURITY_HEADERS, "X-Accel-Buffering": "no"},
        )

    routes: list[BaseRoute] = [
        Route("/api/me", me),
        Route("/api/board", board_view),
        Route("/api/insights", insights_view),
        Route("/api/category", category_view),
        Route("/api/new", new_chat, methods=["POST"]),
        Route("/api/chats", chats),
        Route("/api/chats/{tid}", chats),
        Route("/api/chats/{tid}/delete", chats, methods=["POST"]),
        Route("/api/push/key", push_key),
        Route("/api/memory", memory_view, methods=["GET", "POST"]),
        Route("/api/kitchen", kitchen_view),
        Route("/api/kitchen/prices/jobs", phone_prices),
        Route("/api/kitchen/prices/item", phone_prices, methods=["POST"]),
        Route("/api/kitchen/prices/done", phone_prices, methods=["POST"]),
        Route("/api/kitchen/photo", food_photo, methods=["POST"]),
        Route("/api/kitchen/receipt", read_receipt, methods=["POST"]),
        Route("/api/kitchen/{action}", kitchen_view, methods=["POST"]),
        Route("/api/memory/delete", memory_view, methods=["POST"]),
        Route("/api/push/subscribe", push_change, methods=["POST"]),
        Route("/api/push/unsubscribe", push_change, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
        Mount("/", app=StaticFiles(directory=STATIC_DIR, html=True), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(SecurityHeaders)])


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _snapshot() -> str:
    """Household snapshot for a new conversation ('' if the database is not ready)."""
    try:
        return str(GranaTools(config.db_path()).household_context()["text"])
    except (ToolError, sqlite3.Error, OSError):
        log.warning("could not build the household snapshot")
        return ""


def _record_usage(user: str, event: dict[str, Any], tools: int) -> None:
    """Best effort: a usage row must never break the answer."""
    try:
        conn = db.connect(config.db_path())
        try:
            usage.record(conn, user, event["type"] == "done", tools, event.get("usage"))
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        log.warning("could not record chat usage")


MAX_PRICE_BODY = 1024 * 1024  # one item: up to 80 products


def meal_now() -> str:
    """The meal of the hour in São Paulo (a photo is usually taken while eating)."""
    h = datetime.now(ZoneInfo("America/Sao_Paulo")).hour
    return "cafe" if h < 11 else "almoco" if h < 15 else "lanche" if h < 18 else "jantar"


def photo(value: Any) -> tuple[str, str] | None:
    """A data: URL from the phone -> (media type, base64), checked; None if absent.
    Raises ValueError for anything that isn't a small JPEG/PNG/WebP."""
    if value is None or value == "":
        return None
    m = _DATA_URL.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise ValueError("foto inválida")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except binascii.Error:
        raise ValueError("foto inválida") from None
    if len(raw) > MAX_IMAGE or not raw.startswith(_MAGIC[m.group(1)]):
        raise ValueError("foto inválida")
    return m.group(1), m.group(2)


HEARTBEAT = b": ping\n\n"  # SSE comment: ignored by the page, keeps proxies awake
HEARTBEAT_S = 10.0


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
