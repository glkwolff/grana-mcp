"""Water: how much each person drank today vs. a daily goal, and the reminder that nudges
whoever is behind (only their own phone, only while they are behind and haven't drunk for
a while, inside their own hours).

The goal is agreed with each person in the chat and saved; until then there is no goal and
no reminder. 35 ml per kg of the latest weigh-in (1.5-4 l) is only a starting suggestion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from grana_mcp.kitchen import store
from grana_mcp.kitchen.store import KitchenError
from grana_mcp.store.db import now_utc

TZ = ZoneInfo("America/Sao_Paulo")
ML_PER_KG = 35
AUTO_MIN, AUTO_MAX = 1500, 4000
MAX_SIP = 3000
WEIGHT_DAYS = 120
BEHIND_ML = 250  # "behind the pace" only from this much
DEFAULTS = {"reminders": 0, "start_hour": 8, "end_hour": 22, "every_min": 120}


def liters(ml: int) -> str:
    return (
        f"{ml} ml" if ml < 1000 else f"{ml / 1000:.1f}".replace(".", ",").replace(",0", "") + " L"
    )


def _settings(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {str(r["person_id"]): r for r in conn.execute("SELECT * FROM water_settings")}


def _weights(conn: sqlite3.Connection, today: date) -> dict[str, int]:
    latest: dict[str, int] = {}
    for w in store.weights(conn, today - timedelta(days=WEIGHT_DAYS)):  # oldest first
        latest[str(w["person_id"])] = int(w["grams"])
    return latest


def goal(row: sqlite3.Row | None) -> int | None:
    """The goal the person agreed to, or None (not agreed yet)."""
    return int(row["goal_ml"]) if row is not None and row["goal_ml"] else None


def suggestion(grams: int | None) -> int | None:
    """A starting point for the chat: 35 ml per kg of the latest weigh-in (1.5-4 l)."""
    if not grams:
        return None
    ml = int(grams / 1000 * ML_PER_KG / 100 + 0.5) * 100  # half up: 70 kg -> 2.5 l
    return max(AUTO_MIN, min(AUTO_MAX, ml))


def _local(at: str) -> datetime:
    return datetime.fromisoformat(at).astimezone(TZ)


def status(conn: sqlite3.Connection, today: date, me: str | None = None) -> list[dict[str, Any]]:
    """Every household member today: drunk, goal, last sip and reminder settings."""
    settings = _settings(conn)
    weights = _weights(conn, today)
    drunk = {
        str(r[0]): (int(r[1]), str(r[2]), int(r[3]))
        for r in conn.execute(
            "SELECT person_id, SUM(ml), MAX(at), COUNT(*) FROM water_log WHERE day = ? "
            "GROUP BY person_id",
            (today.isoformat(),),
        )
    }
    mine = (me or "").strip().lower()
    out = []
    for pid, name in conn.execute("SELECT id, display_name FROM people ORDER BY display_name"):
        row = settings.get(str(pid))
        ml_goal = goal(row)
        ml, last, n = drunk.get(str(pid), (0, "", 0))
        cfg = {k: (int(row[k]) if row is not None else v) for k, v in DEFAULTS.items()}
        out.append(
            {
                "person": str(name),
                "me": bool(mine) and mine in (str(pid), str(name).lower()),
                "ml": ml,
                "goal_ml": ml_goal,  # None: not agreed yet (combine in the chat)
                "suggested_ml": suggestion(weights.get(str(pid))),
                "pct": min(100, round(ml / ml_goal * 100)) if ml_goal else None,
                "done": ml_goal is not None and ml >= ml_goal,
                "entries": n,
                "last": _local(last).strftime("%H:%M") if last else None,
                "reminders": bool(cfg["reminders"]),
                "start_hour": cfg["start_hour"],
                "end_hour": cfg["end_hour"],
                "every_min": cfg["every_min"],
            }
        )
    return out


def add(conn: sqlite3.Connection, who: str, ml: Any, day: date, by: str | None) -> int:
    try:
        amount = round(float(ml))
    except (TypeError, ValueError):
        raise KitchenError("diga quantos ml") from None
    if not 0 < amount <= MAX_SIP:
        raise KitchenError(f"entre 1 e {MAX_SIP} ml por vez")
    pid = store.person_id(conn, who)
    cur = conn.execute(
        "INSERT INTO water_log (day, person_id, ml, at, by) VALUES (?, ?, ?, ?, ?)",
        (day.isoformat(), pid, amount, now_utc(), by),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def undo_last(conn: sqlite3.Connection, who: str, day: date) -> int:
    """Remove the person's latest entry of the day; returns its ml (0 if none)."""
    pid = store.person_id(conn, who)
    row = conn.execute(
        "SELECT id, ml FROM water_log WHERE person_id = ? AND day = ? ORDER BY at DESC, id DESC "
        "LIMIT 1",
        (pid, day.isoformat()),
    ).fetchone()
    if not row:
        return 0
    conn.execute("DELETE FROM water_log WHERE id = ?", (row[0],))
    conn.commit()
    return int(row[1])


def configure(
    conn: sqlite3.Connection,
    who: str,
    by: str | None,
    goal_ml: Any = None,
    reminders: bool | None = None,
    start_hour: Any = None,
    end_hour: Any = None,
    every_min: Any = None,
) -> str:
    """Change only what was given (the goal as agreed in the chat)."""
    pid = store.person_id(conn, who)
    cur = _settings(conn).get(pid)
    vals: dict[str, Any] = {k: (cur[k] if cur is not None else v) for k, v in DEFAULTS.items()}
    vals["goal_ml"] = cur["goal_ml"] if cur is not None else None

    def num(value: Any, lo: int, hi: int, what: str) -> int:
        try:
            n = round(float(value))
        except (TypeError, ValueError):
            raise KitchenError(f"{what} inválido") from None
        if not lo <= n <= hi:
            raise KitchenError(f"{what}: entre {lo} e {hi}")
        return n

    if goal_ml is not None:
        vals["goal_ml"] = num(goal_ml, 500, 8000, "meta (ml)")
    if reminders is not None:
        vals["reminders"] = 1 if reminders else 0
    if vals["reminders"] and not vals["goal_ml"]:
        raise KitchenError("primeiro combinem a meta de água no chat; o lembrete usa a meta")
    if start_hour is not None:
        vals["start_hour"] = num(start_hour, 0, 23, "hora de começar")
    if end_hour is not None:
        vals["end_hour"] = num(end_hour, 1, 24, "hora de parar")
    if every_min is not None:
        vals["every_min"] = num(every_min, 30, 480, "intervalo (min)")
    if vals["end_hour"] <= vals["start_hour"]:
        raise KitchenError("a hora de parar precisa ser depois da de começar")
    conn.execute(
        "INSERT INTO water_settings (person_id, goal_ml, reminders, start_hour, end_hour, "
        "every_min, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (person_id) DO UPDATE SET goal_ml = excluded.goal_ml, "
        "reminders = excluded.reminders, start_hour = excluded.start_hour, "
        "end_hour = excluded.end_hour, every_min = excluded.every_min, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (
            pid,
            vals["goal_ml"],
            vals["reminders"],
            vals["start_hour"],
            vals["end_hour"],
            vals["every_min"],
            now_utc(),
            by,
        ),
    )
    conn.commit()
    return pid


@dataclass(frozen=True, slots=True)
class Reminder:
    key: str
    person_id: str
    title: str
    body: str
    to: tuple[str, ...]  # web users who are this person (their phones only)


def due(conn: sqlite3.Connection, now: datetime) -> list[Reminder]:
    """Reminders due at ``now`` (local time): reminders on, inside the person's hours,
    below the goal and nothing drunk for ``every_min``. One per interval at most (the key)."""
    now = now.astimezone(TZ) if now.tzinfo else now.replace(tzinfo=TZ)
    today = now.date()
    settings = _settings(conn)
    if not any(r["reminders"] for r in settings.values()):
        return []
    names = {str(r[0]): str(r[1]) for r in conn.execute("SELECT id, display_name FROM people")}
    out = []
    for pid, row in settings.items():
        if not row["reminders"] or pid not in names:
            continue
        start = now.replace(hour=int(row["start_hour"]), minute=0, second=0, microsecond=0)
        end_h = int(row["end_hour"])
        end = start.replace(hour=0) + timedelta(hours=end_h)
        if not start <= now < end:
            continue
        ml_goal = goal(row)
        if not ml_goal:
            continue
        drunk, last = conn.execute(
            "SELECT COALESCE(SUM(ml), 0), MAX(at) FROM water_log WHERE person_id = ? AND day = ?",
            (pid, today.isoformat()),
        ).fetchone()
        if drunk >= ml_goal:
            continue
        every = int(row["every_min"])
        since = max(start, _local(last)) if last else start
        if now - since < timedelta(minutes=every):
            continue
        elapsed = (now - start).total_seconds() / 60
        window = (end - start).total_seconds() / 60
        pace = round(ml_goal * min(1.0, elapsed / window))
        body = f"Faltam {liters(ml_goal - int(drunk))} para a meta de {liters(ml_goal)} hoje."
        if pace - drunk >= BEHIND_ML:
            body += f" Uns {liters(round((pace - drunk) / 50) * 50)} atrás do ritmo."
        body += " Bora um copo? Toque para anotar."
        name = names[pid]
        out.append(
            Reminder(
                key=f"water:{pid}:{today.isoformat()}:{int(elapsed // every)}",
                person_id=pid,
                title=f"💧 Água, {name.split()[0]}",
                body=body,
                to=(pid, name.lower()),
            )
        )
    return out
