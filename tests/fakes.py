"""A fake Pluggy API on ``httpx.MockTransport``, serving 100% synthetic fixtures.

Response shapes follow the documented API (OpenAPI at docs.pluggy.ai):
``POST /auth``, ``GET /items/{id}``, ``GET /accounts?itemId=``,
``GET /v2/transactions?accountId=`` with ``next`` cursor query strings,
``GET /bills?accountId=`` with ``page``/``totalPages``, ``GET /identity?itemId=``
(404 ``IDENTITY_NOT_FOUND`` unless ``identity`` is set).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from grana_mcp.providers.pluggy import PluggyClient

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"

CLIENT_ID = "synthetic-client-id"
CLIENT_SECRET = "synthetic-client-secret-DO-NOT-PRINT"
ITEM_ID = "11111111-1111-4111-8111-000000000001"
CHECKING_ID = "22222222-2222-4222-8222-000000000001"
CARD_ID = "22222222-2222-4222-8222-000000000002"
CURSOR_1 = "c3ludGhldGljLWN1cnNvci0x"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakePluggy:
    item: dict[str, Any] = field(default_factory=lambda: load("item.json"))
    accounts: dict[str, Any] = field(default_factory=lambda: load("accounts.json"))
    # accountId -> {cursor or "" for first page: page body}
    pages: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            CHECKING_ID: {
                "": load("transactions_checking_page1.json"),
                CURSOR_1: load("transactions_checking_page2.json"),
            },
            CARD_ID: {"": load("transactions_card.json")},
        }
    )
    # accountId -> list of pages (page 1 first)
    bills: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {CARD_ID: [load("bills_card.json")]}
    )
    bills_status: int = 200
    # GET /identity body (None -> documented 404 IDENTITY_NOT_FOUND)
    identity: dict[str, Any] | None = None
    identity_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    issued_keys: list[str] = field(default_factory=list)
    expired_keys: set[str] = field(default_factory=set)
    rate_limit_next: int = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return httpx.Response(
                429,
                json={"code": 429, "message": "Too many requests"},
                headers={"Retry-After": "60", "RateLimit-Reset": "2"},
            )
        if request.method == "POST" and path == "/auth":
            body = json.loads(request.content)
            if body != {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}:
                return httpx.Response(
                    401,
                    json={
                        "code": 401,
                        "codeDescription": "CLIENT_KEYS_UNAUTHORIZED",
                        "message": "Client keys are invalid",
                    },
                )
            key = f"synthetic-api-key-{len(self.issued_keys) + 1}"
            self.issued_keys.append(key)
            return httpx.Response(200, json={"apiKey": key})

        key = request.headers.get("X-API-KEY")
        if request.method != "GET" or key not in self.issued_keys or key in self.expired_keys:
            return httpx.Response(401, json={"code": 401, "message": "Unauthorized"})

        if path == f"/items/{self.item['id']}":
            return httpx.Response(200, json=self.item)
        if path.startswith("/items/"):
            return httpx.Response(
                404, json={"code": 404, "codeDescription": "ITEM_NOT_FOUND", "message": "x"}
            )
        if path == "/accounts":
            if request.url.params.get("itemId") != self.item["id"]:
                return httpx.Response(
                    200, json={"page": 1, "total": 0, "totalPages": 0, "results": []}
                )
            return httpx.Response(200, json=self.accounts)
        if path == "/v2/transactions":
            account_pages = self.pages.get(request.url.params.get("accountId", ""), {})
            page = account_pages.get(request.url.params.get("after", ""))
            if page is None:
                return httpx.Response(200, json={"results": [], "next": None})
            # Serve the raw text so decimals reach the client exactly as written.
            return httpx.Response(
                200, content=json.dumps(page).encode(), headers={"content-type": "application/json"}
            )
        if path == "/identity":
            if self.identity_status != 200:
                return httpx.Response(
                    self.identity_status, json={"code": self.identity_status, "message": "x"}
                )
            if self.identity is None or request.url.params.get("itemId") != self.item["id"]:
                return httpx.Response(
                    404,
                    json={
                        "code": 404,
                        "codeDescription": "IDENTITY_NOT_FOUND",
                        "message": "identity not found",
                    },
                )
            return httpx.Response(200, json=self.identity)
        if path == "/bills":
            if self.bills_status != 200:
                return httpx.Response(
                    self.bills_status, json={"code": self.bills_status, "message": "x"}
                )
            pages = self.bills.get(request.url.params.get("accountId", ""), [])
            index = int(request.url.params.get("page", "1")) - 1
            if index >= len(pages):
                return httpx.Response(
                    200, json={"page": index + 1, "total": 0, "totalPages": 0, "results": []}
                )
            return httpx.Response(
                200,
                content=json.dumps(pages[index]).encode(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, json={"code": 404, "message": "not found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self, **kw: Any) -> PluggyClient:
        kw.setdefault("sleep", lambda _s: None)
        return PluggyClient(CLIENT_ID, CLIENT_SECRET, transport=self.transport(), **kw)

    def paths(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]
