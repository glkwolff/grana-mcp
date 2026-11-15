"""Starlette app for the chat page. Every API call needs ``Authorization: Bearer``."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from grana_mcp import config
from grana_mcp.notify import push
from grana_mcp.notify import service as notify
from grana_mcp.server import GranaTools
from grana_mcp.store import db
from grana_mcp.web import auth
from grana_mcp.web.claude_runner import RunnerConfig, prepare, run_turn
from grana_mcp.web.limits import AuthGuard, ChatLimits

MAX_MESSAGE = 2000
STATIC_DIR = Path(__file__).parent / "static"  # built by web-ui/ (npm run build)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class State:
    runner: RunnerConfig
    limits: ChatLimits = field(default_factory=ChatLimits)
    guard: AuthGuard = field(default_factory=AuthGuard)
    sessions: dict[str, Session] = field(default_factory=dict)

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


def create_app(runner: RunnerConfig | None = None, limits: ChatLimits | None = None) -> Starlette:
    state = State(runner or RunnerConfig(), limits or ChatLimits())
    prepare(state.runner)

    async def me(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        return JSONResponse({"user": user}, headers=SECURITY_HEADERS)

    async def board_view(request: Request) -> Response:
        try:
            await _user(request, state)
        except Denied as d:
            return d.response
        try:
            data = GranaTools(config.db_path()).board()
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503, headers=SECURITY_HEADERS)
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

    async def new_chat(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        state.sessions.pop(user, None)
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    async def chat(request: Request) -> Response:
        try:
            user = await _user(request, state)
        except Denied as d:
            return d.response
        try:
            body = await request.json()
            message = str(body.get("message", "")).strip()
        except (ValueError, AttributeError):
            message = ""
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
        wait = state.limits.take(user)
        if wait:
            return _too_many(
                f"limite de perguntas atingido ({state.limits.per_hour}/hora, "
                f"{state.limits.per_day}/dia); tente de novo em {wait // 60 + 1} min",
                wait,
            )

        async def stream() -> AsyncIterator[bytes]:
            async with session.lock:
                resume = session.started
                session.started = True
                async for event in run_turn(
                    state.runner, message, user, session.session_id, resume
                ):
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
        Route("/api/push/key", push_key),
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


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
