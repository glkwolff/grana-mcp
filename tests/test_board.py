from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from grana_mcp import board, config
from grana_mcp.server import GranaTools
from grana_mcp.store import db

TODAY = date(2026, 11, 15)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO profiles VALUES ('casa', 'Casa', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('i', 'casa');"
        "INSERT INTO people (id, display_name, created_at) VALUES ('ana', 'Ana', 'x'),"
        "('bia', 'Bia', 'x');"
        "INSERT INTO accounts (id, item_id, kind, name, owner_person_id) VALUES "
        "('a1', 'i', 'checking', 'Conta A', 'ana'), ('b1', 'i', 'checking', 'Conta B', 'bia');"
    )
    for i, (acc, d, cents, cat) in enumerate(
        [
            ("a1", "2026-11-03", -30000, "Mercado"),
            ("b1", "2026-11-05", -20000, "Mercado"),
            ("b1", "2026-11-06", -5000, "Delivery"),
            ("a1", "2026-10-03", -99900, "Mercado"),  # last month
            ("a1", "2026-11-20", -1000, "Mercado"),  # future: ignored
        ]
    ):
        c.execute(
            "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
            "description_raw, category, source, synced_at) "
            "VALUES (?, ?, 'casa', ?, ?, 'SINTETICO', ?, 'pluggy', 'x')",
            (f"t{i}", acc, d, cents, cat),
        )
    c.commit()
    yield c
    c.close()


def tools() -> GranaTools:
    return GranaTools(config.db_path(), lambda: TODAY)


def test_budgets_track_month_spending(conn: sqlite3.Connection) -> None:
    assert board.set_budget(conn, "Mercado", 800) == "criado"
    assert board.set_budget(conn, "mercado", 1000, note="feira inclusa") == "atualizado"
    assert board.set_budget(conn, "Mercado", 300, person="Bia") == "criado"
    status = {(b.category, b.person): b for b in board.budgets(conn, TODAY)}
    house = status[("Mercado", None)]
    assert (house.limit_cents, house.spent_cents, house.pct) == (100000, 50000, 50.0)
    assert house.note == "feira inclusa"
    assert status[("Mercado", "Bia")].spent_cents == 20000
    assert board.set_budget(conn, "Mercado", 0, person="bia") == "removido"
    assert board.set_budget(conn, "Pets", 0) == "não existia"


def test_goals_create_update_add_and_delete(conn: sqlite3.Connection) -> None:
    with pytest.raises(board.BoardError, match="alvo"):
        board.set_goal(conn, "Viagem")
    assert board.set_goal(conn, "Viagem", target=5000, deadline="2027-06-30") == "criado"
    assert board.set_goal(conn, "viagem", add_saved=1200.5) == "atualizado"
    assert board.set_goal(conn, "Viagem", add_saved=-300) == "atualizado"
    [g] = board.goals(conn)
    assert (g["target_cents"], g["saved_cents"], g["deadline"]) == (500000, 90050, "2027-06-30")
    with pytest.raises(board.BoardError, match="prazo"):
        board.set_goal(conn, "Viagem", deadline="junho")
    assert board.delete_goal(conn, "VIAGEM") and not board.delete_goal(conn, "Viagem")


def test_notices_expire_and_validate(conn: sqlite3.Connection) -> None:
    keep = board.post_notice(conn, "Conta de luz vence dia 20", "warn", actor="ana")
    board.post_notice(conn, "Antigo", expires_on="2026-11-01")
    shown = board.notices(conn, TODAY)
    assert [n["id"] for n in shown] == [keep]
    assert shown[0]["created_by"] == "ana"
    with pytest.raises(board.BoardError):
        board.post_notice(conn, "x", "urgente")
    with pytest.raises(board.BoardError):
        board.post_notice(conn, "   ")
    assert board.delete_notice(conn, keep) and not board.delete_notice(conn, keep)


def test_text_is_sanitized_and_limited(conn: sqlite3.Connection) -> None:
    board.post_notice(conn, "<script>alert(1)</script>\x00\x1b[31m" + "a" * 500)
    [n] = board.notices(conn, TODAY)
    assert "\x00" not in n["text"] and "\x1b" not in n["text"]
    assert len(n["text"]) <= board.MAX_NOTICE


def test_invalid_inputs(conn: sqlite3.Connection) -> None:
    with pytest.raises(board.BoardError, match="não existe"):
        board.set_budget(conn, "Mercado", 100, person="Ze")
    with pytest.raises(board.BoardError):
        board.set_budget(conn, "Mercado", -5)
    with pytest.raises(board.BoardError):
        board.set_budget(conn, "Mercado", "muito")  # type: ignore[arg-type]
    with pytest.raises(board.BoardError):
        board.set_budget(conn, "", 100)


def test_limits_on_number_of_items(conn: sqlite3.Connection) -> None:
    for i in range(board.MAX_ITEMS):
        board.post_notice(conn, f"aviso {i}")
    with pytest.raises(board.BoardError, match="máximo"):
        board.post_notice(conn, "mais um")


def test_board_tool_and_writers(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRANA_ACTOR", "bia")
    assert tools().set_budget("Mercado", 1000)["ok"]
    assert tools().set_goal("Reserva", target=10000, saved=2500)["ok"]
    out = tools().post_notice("Revisar assinaturas", "info")
    assert out["ok"]
    with pytest.raises(ToolError):
        tools().post_notice("x", "nope")
    b = tools().board()
    assert b["month"]["total"]["cents"] == 55000
    assert b["month"]["previous_same_period"]["cents"] == 99900
    assert b["budgets"][0]["spent"]["cents"] == 50000 and b["budgets"][0]["pct"] == 50.0
    assert b["goals"][0]["pct"] == 25.0
    assert b["notices"][0]["by"] == "bia"
    assert {p["key"] for p in b["month"]["by_person"]} == {"Ana", "Bia"}
    nid = b["notices"][0]["id"]
    assert tools().delete_notice(nid)["summary"] == "Aviso removido."
    assert tools().delete_goal("Reserva")["summary"] == "Objetivo removido."
