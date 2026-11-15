from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from grana_mcp import board, config
from grana_mcp.notify import alerts, push, service
from grana_mcp.providers.pluggy import SAO_PAULO
from grana_mcp.store import db
from grana_mcp.web import auth

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=SAO_PAULO)  # a Sunday


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO profiles VALUES ('ana', 'Ana', 'x');"
        "INSERT INTO items (id, profile_id, connector_name, status, consent_expires_at) VALUES "
        "('item-a', 'ana', 'MeuPluggy', 'UPDATED', '2027-10-01T00:00:00+00:00');"
        "INSERT INTO accounts (id, item_id, kind, name, number_masked) VALUES "
        "('chk', 'item-a', 'checking', 'Conta', '****0001');"
    )
    yield c
    c.close()


_n = iter(range(1, 10_000))


def tx(conn: sqlite3.Connection, day: str, cents: int, desc: str = "LOJA SINTETICA",
       category: str = "Mercado", **extra: Any) -> str:  # fmt: skip
    tx_id = f"t{next(_n)}"
    cols = {
        "id": tx_id, "account_id": "chk", "profile_id": "ana", "posted_date": day,
        "amount_cents": cents, "description_raw": desc, "category": category,
        "status": "POSTED", "source": "pluggy", "synced_at": "x", **extra,
    }  # fmt: skip
    conn.execute(
        f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
        list(cols.values()),
    )
    conn.commit()
    return tx_id


def browser() -> tuple[ec.EllipticCurvePrivateKey, bytes, push.Subscription]:
    key = ec.generate_private_key(ec.SECP256R1())
    secret = os.urandom(16)
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    sub = push.Subscription("https://web.push.apple.com/abc", push.b64u(pub), push.b64u(secret))
    return key, secret, sub


# --- push ---


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://web.push.apple.com/QGx", True),
        ("https://fcm.googleapis.com/fcm/send/x", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/x", True),
        ("http://web.push.apple.com/x", False),
        ("https://evil.example/x", False),
        ("https://web.push.apple.com.evil.example/x", False),
        ("https://user@web.push.apple.com/x", False),
        ("https://web.push.apple.com:8443/x", False),
        ("https://web.push.apple.com/" + "x" * 2000, False),
    ],
)
def test_valid_endpoint(url: str, ok: bool) -> None:
    assert push.valid_endpoint(url) is ok


def test_setup_keeps_private_key_in_keyring() -> None:
    pub = push.setup()
    assert push.public_key() == pub
    assert "PRIVATE" not in push.public_key_path().read_text()
    assert push.setup() == pub  # idempotent
    assert push.setup(force=True) != pub
    push.load_vapid()


def test_load_without_setup_fails() -> None:
    with pytest.raises(push.PushError):
        push.load_vapid()


def test_send_encrypts_and_signs() -> None:
    push.setup()
    key, secret, sub = browser()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = request.read()
        return httpx.Response(201)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        status = push.send(client, push.load_vapid(), sub, {"title": "Oi", "body": "R$ 1"})
    assert status == 201
    h = seen["headers"]
    assert h["content-encoding"] == "aes128gcm" and h["ttl"] == "86400"
    assert h["authorization"].startswith("vapid t=") and push.public_key() in h["authorization"]
    plain = http_ece.decrypt(seen["body"], private_key=key, auth_secret=secret, version="aes128gcm")
    assert json.loads(plain) == {"title": "Oi", "body": "R$ 1"}
    assert b"R$ 1" not in seen["body"]


# --- subscriptions ---


def test_subscribe_validates(conn: sqlite3.Connection) -> None:
    _, _, sub = browser()
    service.subscribe(conn, "ana", sub.endpoint, sub.p256dh, sub.auth)
    service.subscribe(conn, "bia", sub.endpoint, sub.p256dh, sub.auth)  # same phone, new user
    assert [r["user"] for r in service.subscriptions(conn)] == ["bia"]
    with pytest.raises(push.PushError):
        service.subscribe(conn, "ana", "https://evil.example/x", sub.p256dh, sub.auth)
    with pytest.raises(push.PushError):
        service.subscribe(conn, "ana", sub.endpoint, "curto", sub.auth)
    assert not service.unsubscribe(conn, "ana", sub.endpoint)  # someone else's
    assert service.unsubscribe(conn, "bia", sub.endpoint)


def test_deliver_drops_gone_subscriptions(conn: sqlite3.Connection) -> None:
    push.setup()
    _, _, a = browser()
    _, _, b = browser()
    b = push.Subscription("https://fcm.googleapis.com/gone", b.p256dh, b.auth)
    for s in (a, b):
        service.subscribe(conn, "ana", s.endpoint, s.p256dh, s.auth)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410 if "gone" in str(request.url) else 201)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ok, bad = service.make_deliver(conn, client)(alerts.Alert("k", "t", "b"))
    assert (ok, bad) == (1, 1)
    rows = service.subscriptions(conn)
    assert [r["endpoint"] for r in rows] == [a.endpoint] and rows[0]["last_ok_at"]


# --- alerts ---


def keys(found: list[alerts.Alert]) -> list[str]:
    return [a.key for a in found]


def test_budget_alerts(conn: sqlite3.Connection) -> None:
    board.set_budget(conn, "Mercado", 1000, None, None, "ana")
    tx(conn, "2026-11-02", -850_00)
    assert keys(alerts.budget_alerts(conn, NOW.date())) == ["budget:2026-11:mercado::80"]
    tx(conn, "2026-11-03", -200_00)
    found = alerts.collect(conn, NOW, [], alerts.Settings(weekly=False, big_purchase_cents=10**9))
    assert "budget:2026-11:mercado::100" in keys(found)
    assert "budget:2026-11:mercado::80" not in keys(found)


def test_big_purchase_masks_people(conn: sqlite3.Connection) -> None:
    tx(conn, "2026-11-14", -600_00, "TV SINTETICA", "Compras")
    tx(conn, "2026-11-14", -499_99, "PEQUENA", "Compras")
    tx(conn, "2026-11-01", -900_00, "ANTIGA", "Compras")  # older than RECENT_DAYS
    tx(conn, "2026-11-14", -900_00, "FATURA", "Compras", excluded_reason="card_bill_payment")
    tx(conn, "2026-11-13", -700_00, "PIX ENVIADO Fulano Sintetico", "Transferências",
       operation_type="PIX", counterpart_doc_hash="h", counterpart_doc_type="CPF")  # fmt: skip
    found = alerts.purchase_alerts(conn, NOW.date(), 500_00, every=False)
    assert len(found) == 2
    text = json.dumps([(a.title, a.body) for a in found], ensure_ascii=False)
    assert "TV SINTETICA" in text and "Fulano" not in text and "R$ 600,00" in text


def test_notice_and_health_alerts(conn: sqlite3.Connection) -> None:
    board.post_notice(conn, "Viagem: segurar gastos", "warn", None, "bia")
    conn.execute(
        "INSERT INTO sync_runs (profile_id, started_at, finished_at, status) "
        "VALUES ('ana', '2026-11-12T10:00:00+00:00', '2026-11-12T10:01:00+00:00', 'ok')"
    )
    conn.execute(
        "UPDATE items SET consent_expires_at = ?", ((NOW + timedelta(days=5)).isoformat(),)
    )
    conn.commit()
    ks = keys(alerts.collect(conn, NOW, [], alerts.Settings(weekly=False)))
    assert any(k.startswith("notice:") for k in ks)
    assert "sync-stale:2026-11-15" in ks
    assert any(k.startswith("consent:item-a:") and k.endswith(":7") for k in ks)


def test_weekly_only_sunday_evening(conn: sqlite3.Connection) -> None:
    tx(conn, "2026-11-12", -100_00)
    tx(conn, "2026-11-05", -50_00)
    assert alerts.weekly_alert(conn, NOW) == []  # 10h
    evening = NOW.replace(hour=20)
    (a,) = alerts.weekly_alert(conn, evening)
    assert a.key == "week:2026-46" and "R$ 100,00" in a.title and "Mercado" in a.body
    assert alerts.weekly_alert(conn, evening - timedelta(days=1)) == []  # Saturday


def card(
    due: str, total: int, paid: int, closing: str | None, estimated: int | None
) -> dict[str, Any]:
    return {
        "account_id": "card-1", "account": "Cartao ****0002", "owner": "Ana",
        "closed_bills": [{"due_date": due, "total": {"cents": total}, "paid": {"cents": paid}}],
        "open_bill_estimate": {
            "closing_date_used": closing,
            "estimated": {"cents": estimated, "brl": "R$ 1.234,00"} if estimated else None,
        },
    }  # fmt: skip


def test_bill_alerts() -> None:
    today = NOW.date()
    due = alerts.bill_alerts([card("2026-11-17", 1000_00, 0, None, None)], today)
    assert keys(due) == ["bill-due:card-1:2026-11-17"] and "R$ 1.000,00" in due[0].body
    assert alerts.bill_alerts([card("2026-11-17", 1000_00, 1000_00, None, None)], today) == []
    assert alerts.bill_alerts([card("2026-11-25", 1000_00, 0, None, None)], today) == []
    today_due = alerts.bill_alerts([card("2026-11-15", 10_00, 0, None, None)], today)
    assert today_due[0].urgent
    closing = alerts.bill_alerts([card("2026-10-01", 1, 1, "2026-10-17", 1234_00)], today)
    assert keys(closing) == ["bill-close:card-1:2026-11-17"]


def test_recurring_alerts(conn: sqlite3.Connection) -> None:
    for d in ("2026-08-10", "2026-09-10", "2026-10-10"):
        tx(conn, d, -39_90, "STREAMING SINTETICO", "Assinaturas", merchant="STREAMING SINTETICO")
    tx(conn, "2026-11-10", -41_90, "STREAMING SINTETICO", "Assinaturas",
       merchant="STREAMING SINTETICO")  # fmt: skip
    found = alerts.recurring_alerts(conn, NOW.date())
    assert len(found) == 1 and found[0].key.startswith("rec-price:")
    assert "subiu" in found[0].title and "R$ 41,90" in found[0].body


def test_run_bootstraps_dedupes_and_respects_quiet_hours(conn: sqlite3.Connection) -> None:
    sent: list[str] = []

    def deliver(a: alerts.Alert) -> tuple[int, int]:
        sent.append(a.key)
        return 1, 0

    s = alerts.Settings(weekly=False)
    tx(conn, "2026-11-14", -600_00, "ANTES DE ATIVAR", "Compras")
    first = alerts.run(conn, NOW, [], deliver, s)
    assert first.bootstrapped == 1 and sent == []  # the past is not replayed
    new = tx(conn, "2026-11-15", -700_00, "NOVA", "Compras")
    night = NOW.replace(hour=23)
    assert alerts.run(conn, night, [], deliver, s).deferred == 1 and sent == []
    alerts.run(conn, NOW, [], deliver, s)
    assert sent == [f"tx:{new}"]
    alerts.run(conn, NOW, [], deliver, s)
    assert sent == [f"tx:{new}"]  # once only
    for i in range(8):
        tx(conn, "2026-11-15", -(600_00 + i), f"LOTE {i}", "Compras")
    result = alerts.run(conn, NOW, [], deliver, s)
    assert len(result.sent) == alerts.MAX_PER_RUN and result.sent[-1].title == "+4 avisos no grana"
    assert alerts.run(conn, NOW, [], deliver, s).sent == []


def test_urgent_goes_out_at_night(conn: sqlite3.Connection) -> None:
    sent: list[str] = []

    def deliver(a: alerts.Alert) -> tuple[int, int]:
        sent.append(a.key)
        return 1, 0

    alerts.run(conn, NOW, [], deliver, alerts.Settings())
    night = datetime(2026, 11, 17, 23, 0, tzinfo=SAO_PAULO)
    bill = card("2026-11-17", 100_00, 0, None, None)
    alerts.run(conn, night, [bill], deliver, alerts.Settings())
    assert sent == ["bill-due:card-1:2026-11-17"]


def test_dry_run_sends_nothing(conn: sqlite3.Connection) -> None:
    tx(conn, date.today().isoformat(), -600_00, "HOJE", "Compras")
    result = service.run(conn, config.db_path(), alerts.Settings(weekly=False), dry_run=True)
    assert any(a.key.startswith("tx:") for a in result.sent)
    assert conn.execute("SELECT COUNT(*) FROM notify_sent").fetchone()[0] == 0


# --- web ---


def test_push_api_and_serve_header(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from grana_mcp.web import claude_runner as cr
    from grana_mcp.web.app import create_app

    db.connect(config.db_path()).close()
    client = TestClient(create_app(cr.RunnerConfig(claude_bin="/bin/false", workdir=tmp_path)))
    assert client.get("/api/push/key").status_code == 401
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    assert client.get("/api/push/key", headers=h).json() == {"key": None}
    pub = push.setup()
    assert client.get("/api/push/key", headers=h).json() == {"key": pub}
    _, _, sub = browser()
    body = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}
    assert client.post("/api/push/subscribe", json=body, headers=h).json() == {"ok": True}
    bad = {**body, "endpoint": "https://evil.example/x"}
    assert client.post("/api/push/subscribe", json=bad, headers=h).status_code == 400
    assert client.post("/api/push/subscribe", content=b"x", headers=h).status_code == 400
    conn = db.connect(config.db_path())
    assert [r["user"] for r in service.subscriptions(conn)] == ["ana"]
    client.post("/api/push/unsubscribe", json=body, headers=h)
    assert service.subscriptions(conn) == []
    conn.close()

    # tailscale serve header: trusted only from the configured proxy address
    auth.tailscale_add("bia@github", "bia")
    spoof = {auth.LOGIN_HEADER: "bia@github"}
    assert client.get("/api/me", headers=spoof).status_code == 401
    monkeypatch.setenv(auth.PROXY_ENV, "testclient-not-an-ip, 10.0.0.9")
    assert auth.trusted_proxies() == {"10.0.0.9"}
    assert auth.serve_user("10.0.0.9", "BIA@github") == "bia"
    assert auth.serve_user("10.0.0.8", "bia@github") is None
    assert auth.serve_user("10.0.0.9", "outra@github") is None


def test_new_recurring_only_when_just_detected(conn: sqlite3.Connection) -> None:
    for d in ("2026-09-12", "2026-10-12", "2026-11-12"):
        tx(conn, d, -19_90, "APP NOVO SINTETICO", "Assinaturas", merchant="APP NOVO SINTETICO")
    for d in ("2026-06-11", "2026-07-11", "2026-08-11", "2026-09-11", "2026-10-11", "2026-11-11"):
        tx(conn, d, -9_90, "APP ANTIGO SINTETICO", "Assinaturas", merchant="APP ANTIGO SINTETICO")
    found = alerts.recurring_alerts(conn, NOW.date())
    assert [a.title for a in found] == ["Cobrança recorrente: APP NOVO SINTETICO"]


def test_new_purchases_digest(conn: sqlite3.Connection) -> None:
    sent: list[alerts.Alert] = []

    def deliver(a: alerts.Alert) -> tuple[int, int]:
        sent.append(a)
        return 1, 0

    alerts.run(conn, NOW, [], deliver, alerts.Settings(weekly=False))  # bootstrap
    tx(conn, "2026-11-15", -25_00, "PADARIA SINTETICA", "Mercado")
    tx(conn, "2026-11-15", -80_00, "IFD*RESTAURANTE SINTETICO", "Delivery")
    tx(conn, "2026-11-14", -12_00, "ESTACIONAMENTO", "Transporte")
    tx(conn, "2026-11-14", -9_00, "CAFE", "Restaurantes")
    tx(conn, "2026-11-15", 3000_00, "SALARIO", "Receitas")  # income: no alert
    big = tx(conn, "2026-11-15", -800_00, "TV", "Compras")
    alerts.run(conn, NOW, [], deliver, alerts.Settings(weekly=False))
    titles = [a.title for a in sent]
    assert titles == ["Gasto de R$ 800,00", "4 gastos novos · R$ 126,00"]
    assert sent[1].body.startswith("RESTAURANTE SINTETICO R$ 80,00")
    assert "e mais 1" in sent[1].body
    assert sent[0].key == f"tx:{big}"
    alerts.run(conn, NOW, [], deliver, alerts.Settings(weekly=False))
    assert len(sent) == 2  # nothing twice
    tx(conn, "2026-11-15", -30_00, "FARMACIA", "Farmácia")
    alerts.run(conn, NOW, [], deliver, alerts.Settings(weekly=False))
    assert sent[-1].title == "Gasto: R$ 30,00" and sent[-1].url == "/#cat=Farm%C3%A1cia"
    off = alerts.collect(conn, NOW, [], alerts.Settings(weekly=False, purchases=False))
    assert not any(a.group == "buy" for a in off)
