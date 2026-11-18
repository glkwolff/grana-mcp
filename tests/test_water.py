"""Water: goal per person, logging, and the reminder that only goes to that person's phone
when they are behind and haven't drunk for a while."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime

import httpx
import pytest
from starlette.testclient import TestClient

from grana_mcp import config
from grana_mcp.kitchen import store, water
from grana_mcp.kitchen.service import KitchenTools
from grana_mcp.notify import alerts, push, service
from grana_mcp.store import db
from grana_mcp.web import auth
from grana_mcp.web import claude_runner as cr
from grana_mcp.web.app import create_app
from tests.test_notify import browser

TODAY = date(2026, 11, 18)
TZ = water.TZ


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 18, hour, minute, tzinfo=TZ)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO people (id, display_name, created_at) VALUES "
        "('ana', 'Ana', 'x'), ('bia', 'Bia Souza', 'x');"
    )
    yield c
    c.close()


def clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    """Water entries are stamped with now_utc(); pin it."""
    monkeypatch.setattr(water, "now_utc", lambda: when.astimezone(water.TZ).isoformat())


def kitchen(user: str = "ana") -> KitchenTools:
    return KitchenTools(config.db_path(), lambda: TODAY, lambda: user)


def test_no_goal_until_agreed_in_the_chat(conn: sqlite3.Connection) -> None:
    store.log_weight(conn, "ana", 70.0, TODAY, None)
    store.log_weight(conn, "bia", 130.0, TODAY, None)
    people = {p["person"]: p for p in kitchen().water_today()["people"]}
    ana = people["Ana"]
    assert ana["goal_ml"] is None and ana["pct"] is None and not ana["done"]
    assert ana["suggested_ml"] == 2500  # 35 ml/kg, only a starting point for the chat
    assert people["Bia Souza"]["suggested_ml"] == 4000  # capped
    assert ana["me"] and not people["Bia Souza"]["me"]
    assert "Ana 0 ml (meta a combinar)" in kitchen().water_today()["summary"]
    # No goal, no reminders.
    with pytest.raises(store.KitchenError, match="combinem a meta"):
        kitchen().set_water(reminders=True)

    out = kitchen().set_water(goal_ml=3000)
    assert out["summary"] == "Ana: meta de água 3 L, sem lembretes."
    on = kitchen().set_water(reminders=True)
    assert "lembretes a cada 120 min, das 8h às 22h" in on["summary"]
    with pytest.raises(store.KitchenError, match="depois"):
        kitchen().set_water(start_hour=22, end_hour=8)
    with pytest.raises(store.KitchenError, match="intervalo"):
        kitchen().set_water(every_min=5)


def test_kitchen_todo_asks_to_agree_the_water_goal(conn: sqlite3.Connection) -> None:
    kitchen().set_diet_profile("ana", sex="f")
    todo = kitchen().kitchen_status()["todo"]
    assert "Ana: combinar a meta de água (set_water_goal)" in todo
    kitchen().set_water(goal_ml=2000)
    assert not any("Ana: combinar a meta" in t for t in kitchen().kitchen_status()["todo"])


def test_log_and_undo(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    clock(monkeypatch, at(9, 30))
    k = kitchen()
    assert k.log_water(100)["summary"] == "Anotado 100 ml. Ana: 100 ml (meta a combinar)."
    k.undo_water()
    k.set_water(goal_ml=2000)
    out = k.log_water(500)
    assert out["summary"] == "Anotado 500 ml. Ana: 500 ml de 2 L."
    k.log_water(1500)
    done = k.log_water(250, person="Bia Souza")  # someone else, said in the chat
    assert "Bia Souza: 250 ml" in done["summary"]
    ana = next(p for p in k.water_today()["people"] if p["person"] == "Ana")
    assert ana["ml"] == 2000 and ana["done"] and ana["last"] == "09:30" and ana["entries"] == 2
    assert "meta batida" in k.log_water(100)["summary"]
    assert "Tirei o último registro (100 ml)" in k.undo_water()["summary"]
    with pytest.raises(store.KitchenError):
        k.log_water(0)
    with pytest.raises(store.KitchenError):
        k.log_water(5000)
    # food_diary carries the water too, for the chat.
    assert any(p["person"] == "Ana" and p["ml"] == 2000 for p in k.food_diary()["water"])


def test_reminder_only_when_behind_and_quiet_for_a_while(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    k = kitchen()
    assert water.due(conn, at(11)) == []  # reminders are off by default
    k.set_water(goal_ml=2000, reminders=True, every_min=120)  # 8h-22h

    assert water.due(conn, at(7, 30)) == []  # before her hours
    assert water.due(conn, at(9, 45)) == []  # not 2 h into the day yet
    (r,) = water.due(conn, at(10, 0))
    assert r.key == "water:ana:2026-11-18:1" and r.to == ("ana", "ana")
    assert r.title == "💧 Água, Ana"
    assert r.body.startswith("Faltam 2 L para a meta de 2 L hoje. Uns 300 ml atrás do ritmo.")

    clock(monkeypatch, at(10, 30))
    k.log_water(300)
    assert water.due(conn, at(12, 0)) == []  # drank 1h30 ago
    (later,) = water.due(conn, at(12, 30))
    assert later.key.endswith(":2") and "Faltam 1,7 L" in later.body
    assert water.due(conn, at(22, 0)) == []  # after her hours

    clock(monkeypatch, at(13, 0))
    k.log_water(1700)
    assert water.due(conn, at(16, 0)) == []  # goal reached


def test_water_alert_goes_only_to_that_persons_phones(conn: sqlite3.Connection) -> None:
    kitchen("bia").set_water(goal_ml=2500, reminders=True, every_min=60)
    push.setup()
    _, _, ana_phone = browser()
    _, _, bia_phone = browser()
    service.subscribe(conn, "ana", ana_phone.endpoint, ana_phone.p256dh, ana_phone.auth)
    service.subscribe(conn, "bia", bia_phone.endpoint, bia_phone.p256dh, bia_phone.auth)
    alerts._mark(conn, [alerts.BOOTSTRAP_KEY])

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(201)

    now = at(10)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = alerts.run(conn, now, [], service.make_deliver(conn, client))
    water_sent = [a for a in result.sent if a.key.startswith("water:")]
    assert [a.key for a in water_sent] == ["water:bia:2026-11-18:2"]
    assert water_sent[0].url == "/#agua"
    assert sent == [bia_phone.endpoint]
    # Same interval: not again.
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        again = alerts.run(conn, now, [], service.make_deliver(conn, client))
    assert not [a for a in again.sent if a.key.startswith("water:")]


def test_water_route(conn: sqlite3.Connection, tmp_path: object) -> None:
    cfg = cr.RunnerConfig(claude_bin="/bin/false", workdir=config.data_dir() / "w")
    client = TestClient(create_app(cfg))
    h = {"Authorization": f"Bearer {auth.add_user('bia')}"}
    r = client.post("/api/water", json={"ml": 300}, headers=h)
    assert r.status_code == 200, r.text
    me = next(p for p in r.json()["people"] if p["me"])
    assert me["person"] == "Bia Souza" and me["ml"] == 300
    no_goal = client.post("/api/water", json={"settings": {"reminders": True}}, headers=h)
    assert no_goal.status_code == 400 and "combinem a meta" in no_goal.json()["error"]
    kitchen("bia").set_water(goal_ml=2000)  # agreed in the chat
    # the page can't set the goal, only the reminders
    s = client.post(
        "/api/water",
        json={"settings": {"goal_ml": 500, "reminders": True, "every_min": 90}},
        headers=h,
    )
    mine = next(p for p in s.json()["people"] if p["me"])
    assert mine["every_min"] == 90 and mine["goal_ml"] == 2000
    u = client.post("/api/water", json={"undo": True}, headers=h)
    assert next(p for p in u.json()["people"] if p["me"])["ml"] == 0
    assert client.post("/api/water", json={"ml": "muito"}, headers=h).status_code == 400
    assert client.get("/api/water").status_code == 401
    board = client.get("/api/board", headers=h)
    assert board.status_code in (200, 503)
