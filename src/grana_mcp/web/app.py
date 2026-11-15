"""Starlette app for the chat page. Every API call needs ``Authorization: Bearer``."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from grana_mcp.web import auth
from grana_mcp.web.claude_runner import RunnerConfig, prepare, run_turn

MAX_MESSAGE = 2000
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
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
    sessions: dict[str, Session] = field(default_factory=dict)

    def session(self, user: str) -> Session:
        return self.sessions.setdefault(user, Session())


async def _user(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    user = auth.verify(token)
    if user is None:
        await asyncio.sleep(0.5)  # slow down guessing (tokens are 256-bit anyway)
    return user


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "não autorizado"}, status_code=401, headers=SECURITY_HEADERS)


def create_app(runner: RunnerConfig | None = None) -> Starlette:
    state = State(runner or RunnerConfig())
    prepare(state.runner)

    async def static(request: Request) -> Response:
        name, media = STATIC[request.url.path]
        body = resources.files("grana_mcp.web.static").joinpath(name).read_bytes()
        return Response(body, media_type=media, headers=SECURITY_HEADERS)

    async def me(request: Request) -> Response:
        user = await _user(request)
        if user is None:
            return _unauthorized()
        return JSONResponse({"user": user}, headers=SECURITY_HEADERS)

    async def new_chat(request: Request) -> Response:
        user = await _user(request)
        if user is None:
            return _unauthorized()
        state.sessions.pop(user, None)
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    async def chat(request: Request) -> Response:
        user = await _user(request)
        if user is None:
            return _unauthorized()
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

    routes = [Route(path, static) for path in STATIC]
    routes += [
        Route("/api/me", me),
        Route("/api/new", new_chat, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
