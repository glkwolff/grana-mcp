"""Food diary: what each person ate, the day's calories against their target, streaks and
the household's weekly score (cooperative: both add to the same total).

Entries are estimates (from a photo or a description), never stored with the photo.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from grana_mcp.kitchen.store import MEALS, KitchenError, person_id
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

LOG_MEALS = (*MEALS, "extra")
SOURCES = ("foto", "texto", "cardapio")
BAND = (0.9, 1.1)  # "on target": 90-110% of the day's kcal target
LOGGED_MIN = 2  # entries for a day to count as "registered"
STREAK_DAYS = 120  # how far back streaks look
MAX_KCAL = 6000

# Weekly score (Monday to today)
PTS_LOGGED = 1  # per person per day with LOGGED_MIN entries
PTS_ON_TARGET = 2  # per person per day inside BAND
PTS_FOLLOWED = 1  # per day the house said it followed the menu
PTS_WEIGHED = 2  # per person who weighed in that week
PTS_DELIVERY = 5  # delivery orders within the week's limit


def _meal(value: str) -> str:
    meal = str(value or "").strip().lower().replace("é", "e").replace("ç", "c")
    if meal not in LOG_MEALS:
        raise KitchenError("meal: cafe, almoco, lanche, jantar ou extra")
    return meal


def log_meal(
    conn: sqlite3.Connection,
    person: str,
    meal: str,
    descr: str,
    kcal: Any,
    protein_g: Any,
    day: date,
    source: str,
    by: str | None,
) -> tuple[int, str]:
    pid = person_id(conn, person)
    m = _meal(meal)
    text = sanitize_text(descr, 120)
    if not text:
        raise KitchenError("descreva o que comeu")
    try:
        k = round(float(kcal))
        p = round(float(protein_g)) if protein_g is not None else None
    except (TypeError, ValueError):
        raise KitchenError("kcal e protein_g são números") from None
    if not 0 <= k <= MAX_KCAL:
        raise KitchenError(f"kcal entre 0 e {MAX_KCAL}")
    if p is not None and not 0 <= p <= 400:
        raise KitchenError("protein_g entre 0 e 400")
    if source not in SOURCES:
        raise KitchenError("source: foto, texto ou cardapio")
    cur = conn.execute(
        "INSERT INTO meal_log (day, person_id, meal, descr, kcal, protein_g, source, "
        "created_at, by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day.isoformat(), pid, m, text, k, p, source, now_utc(), by),
    )
    conn.commit()
    return int(cur.lastrowid or 0), text


def delete_meal(conn: sqlite3.Connection, meal_id: int) -> str:
    row = conn.execute("SELECT descr, day FROM meal_log WHERE id = ?", (meal_id,)).fetchone()
    if not row:
        raise KitchenError("registro não encontrado")
    conn.execute("DELETE FROM meal_log WHERE id = ?", (meal_id,))
    conn.commit()
    return f"Apagado: {row[0]} ({row[1][8:10]}/{row[1][5:7]})."


def entries(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT m.id, p.display_name AS name, m.meal, m.descr, m.kcal, m.protein_g, m.source "
        "FROM meal_log m JOIN people p ON p.id = m.person_id WHERE m.day = ? "
        "ORDER BY CASE m.meal WHEN 'cafe' THEN 0 WHEN 'almoco' THEN 1 WHEN 'lanche' THEN 2 "
        "WHEN 'jantar' THEN 3 ELSE 4 END, m.id",
        (day.isoformat(),),
    ).fetchall()


@dataclass(frozen=True, slots=True)
class DayTotal:
    kcal: int
    protein_g: int
    count: int


def totals(conn: sqlite3.Connection, since: date, until: date) -> dict[tuple[str, str], DayTotal]:
    """(person name, day) -> the day's totals."""
    rows = conn.execute(
        "SELECT p.display_name, m.day, SUM(m.kcal), SUM(COALESCE(m.protein_g, 0)), COUNT(*) "
        "FROM meal_log m JOIN people p ON p.id = m.person_id WHERE m.day BETWEEN ? AND ? "
        "GROUP BY m.person_id, m.day",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    return {(str(r[0]), str(r[1])): DayTotal(int(r[2]), int(r[3]), int(r[4])) for r in rows}


def on_target(t: DayTotal | None, target: int | None) -> bool:
    return bool(t and target and BAND[0] * target <= t.kcal <= BAND[1] * target)


def logged(t: DayTotal | None) -> bool:
    return bool(t and t.count >= LOGGED_MIN)


def _run(ok: list[bool]) -> tuple[int, int]:
    """(current run ending at the last day, best run). The last day (today) counts only
    when it already qualifies: a day in progress doesn't break the streak."""
    best = cur = 0
    for v in ok:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    current = cur
    if ok and not ok[-1]:
        current = 0
        for v in reversed(ok[:-1]):
            if not v:
                break
            current += 1
    return current, best


def streaks(
    conn: sqlite3.Connection, people: list[dict[str, Any]], today: date
) -> list[dict[str, Any]]:
    since = today - timedelta(days=STREAK_DAYS)
    tot = totals(conn, since, today)
    days = [(since + timedelta(days=i)).isoformat() for i in range(STREAK_DAYS + 1)]
    out = []
    for p in people:
        name, target = p["person"], p.get("kcal")
        reg = [logged(tot.get((name, d))) for d in days]
        hit = [on_target(tot.get((name, d)), target) for d in days]
        cur_reg, best_reg = _run(reg)
        cur_hit, best_hit = _run(hit)
        out.append(
            {
                "person": name,
                "logging_days": cur_reg,
                "logging_best": best_reg,
                "on_target_days": cur_hit if target else None,
                "on_target_best": best_hit if target else None,
            }
        )
    return out


def week_score(
    conn: sqlite3.Connection,
    people: list[dict[str, Any]],
    monday: date,
    until: date,
    delivery: tuple[int, int | None] | None = None,
) -> dict[str, Any]:
    """Points of the household from ``monday`` to ``until`` (inclusive), with the breakdown.
    ``delivery``: (orders that week, weekly limit)."""
    tot = totals(conn, monday, until)
    days = [(monday + timedelta(days=i)).isoformat() for i in range((until - monday).days + 1)]
    count: dict[str, int] = defaultdict(int)
    for p in people:
        for d in days:
            t = tot.get((p["person"], d))
            count["logged"] += logged(t)
            count["on_target"] += on_target(t, p.get("kcal"))
    count["followed"] = conn.execute(
        "SELECT COUNT(*) FROM day_log WHERE followed = 1 AND day BETWEEN ? AND ?",
        (monday.isoformat(), until.isoformat()),
    ).fetchone()[0]
    count["weighed"] = conn.execute(
        "SELECT COUNT(DISTINCT person_id) FROM weight_log WHERE day BETWEEN ? AND ?",
        (monday.isoformat(), until.isoformat()),
    ).fetchone()[0]
    lines = [
        ("dias com refeições registradas", count["logged"], PTS_LOGGED),
        ("dias dentro da meta de kcal", count["on_target"], PTS_ON_TARGET),
        ("dias seguindo o cardápio", count["followed"], PTS_FOLLOWED),
        ("pesagens na semana", count["weighed"], PTS_WEIGHED),
    ]
    breakdown = [{"what": w, "count": n, "points": n * pts} for w, n, pts in lines]
    if delivery and delivery[1] is not None:
        within = delivery[0] <= delivery[1]
        breakdown.append(
            {
                "what": f"delivery dentro do limite ({delivery[0]} de {delivery[1]})",
                "count": int(within),
                "points": PTS_DELIVERY if within else 0,
            }
        )
    n_days, n_people = len(days), len(people)
    best = n_days * n_people * (PTS_LOGGED + PTS_ON_TARGET) + n_days * PTS_FOLLOWED
    best += n_people * PTS_WEIGHED + (PTS_DELIVERY if delivery and delivery[1] is not None else 0)
    return {
        "from": monday.isoformat(),
        "until": until.isoformat(),
        "points": sum(b["points"] for b in breakdown),
        "max_so_far": best,
        "breakdown": breakdown,
    }
