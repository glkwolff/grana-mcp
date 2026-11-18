"""Glue between the alerts, the subscriptions table and the push sender."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx

from grana_mcp.notify import alerts, push
from grana_mcp.providers.pluggy import SAO_PAULO as TZ
from grana_mcp.store.db import now_utc

log = logging.getLogger(__name__)


def subscriptions(conn: sqlite3.Connection, user: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT id, user, endpoint, p256dh, auth, created_at, last_ok_at FROM push_subscriptions"
    if user:
        return conn.execute(sql + " WHERE user = ? ORDER BY id", (user,)).fetchall()
    return conn.execute(sql + " ORDER BY id").fetchall()


def subscribe(conn: sqlite3.Connection, user: str, endpoint: str, p256dh: str, auth: str) -> None:
    if not push.valid_endpoint(endpoint):
        raise push.PushError("endereço de push não permitido")
    if not push.valid_key(p256dh, 65) or not push.valid_key(auth, 16):
        raise push.PushError("chaves de push inválidas")
    conn.execute(
        "INSERT INTO push_subscriptions (user, endpoint, p256dh, auth, created_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (endpoint) DO UPDATE SET user = excluded.user, "
        "p256dh = excluded.p256dh, auth = excluded.auth",
        (user, endpoint, p256dh, auth, now_utc()),
    )
    conn.commit()


def unsubscribe(conn: sqlite3.Connection, user: str, endpoint: str) -> bool:
    cur = conn.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND user = ?", (endpoint, user)
    )
    conn.commit()
    return cur.rowcount > 0


def make_deliver(
    conn: sqlite3.Connection, client: httpx.Client, user: str | None = None
) -> alerts.Deliver:
    vapid = push.load_vapid()

    def deliver(alert: alerts.Alert) -> tuple[int, int]:
        ok = bad = 0
        message = {"title": alert.title, "body": alert.body, "url": alert.url, "tag": alert.tag}
        for sub in subscriptions(conn, user):
            if alert.to and str(sub["user"]).lower() not in alert.to:
                continue  # a personal reminder: only that person's phones
            s = push.Subscription(sub["endpoint"], sub["p256dh"], sub["auth"])
            try:
                status = push.send(
                    client, vapid, s, message, urgency="high" if alert.urgent else "normal"
                )
            except httpx.HTTPError as exc:
                log.warning("push failed for subscription %s: %s", sub["id"], type(exc).__name__)
                bad += 1
                continue
            if status in (404, 410):  # the browser dropped this subscription
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
                bad += 1
            elif 200 <= status < 300:
                conn.execute(
                    "UPDATE push_subscriptions SET last_ok_at = ? WHERE id = ?",
                    (now_utc(), sub["id"]),
                )
                ok += 1
            else:
                log.warning("push service answered %s for subscription %s", status, sub["id"])
                bad += 1
        conn.commit()
        return ok, bad

    return deliver


def run(
    conn: sqlite3.Connection, db_path: Path, settings: alerts.Settings, *, dry_run: bool = False
) -> alerts.RunResult:
    now = datetime.now(TZ)
    cards = alerts.db_cards(db_path, now.date())
    if dry_run:
        result = alerts.RunResult()
        seen = alerts._sent(conn)
        result.sent = [a for a in alerts.collect(conn, now, cards, settings) if a.key not in seen]
        return result
    with httpx.Client() as client:
        return alerts.run(conn, now, cards, make_deliver(conn, client), settings)


def test(conn: sqlite3.Connection, user: str | None = None) -> tuple[int, int]:
    alert = alerts.Alert("_test", "grana: notificações ok", "Assim vão chegar os avisos de gastos.")
    with httpx.Client() as client:
        return make_deliver(conn, client, user)(alert)
