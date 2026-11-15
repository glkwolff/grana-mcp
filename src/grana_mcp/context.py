"""Household snapshot: a compact text the assistant reads before the first question.

Everything here is computed from the local SQLite (no model involved) and kept
short (~1k tokens): month to date vs. the usual, typical spending per category,
budgets, goals, subscriptions, open card bills, a few habits derived from the
data and the notes the household told the assistant (``memory``).

Values are rounded to whole reais; exact figures come from the tools. Merchant
names are third-party text: sanitized, truncated and listed as data.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from grana_mcp import board, memory
from grana_mcp.analytics import queries
from grana_mcp.analytics.periods import Period, month_bounds, shift_month
from grana_mcp.pipeline import recurring
from grana_mcp.privacy.redact import sanitize_text

MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
WEEKDAYS = ["segundas", "terças", "quartas", "quintas", "sextas", "sábados", "domingos"]
EATING_OUT = ("Delivery", "Restaurantes")
MAX_CATEGORIES = 12
MAX_RECURRING = 8
LABEL = 28


def r(cents: int) -> str:
    """R$ in whole reais: 'R$ 1.792'."""
    return "R$ " + f"{round(abs(cents) / 100):,}".replace(",", ".")


def _ddmm(d: date) -> str:
    return d.strftime("%d/%m")


def _month(y: int, m: int) -> Period:
    start, end = month_bounds(y, m)
    return Period(start, end, f"{y:04d}-{m:02d}")


def _closed_months(today: date, n: int) -> list[Period]:
    out = []
    for k in range(n, 0, -1):
        y, m = shift_month(today.year, today.month, -k)
        out.append(_month(y, m))
    return out


def _same_days(p: Period, day: int) -> Period:
    end = p.start.replace(day=min(day, p.end.day))
    return Period(p.start, end, p.label)


def _spending(conn: sqlite3.Connection, today: date) -> list[str]:
    month = _month(today.year, today.month)
    total = queries.spending_total(conn, month, today)
    closed = _closed_months(today, 6)
    totals = [queries.spending_total(conn, p, today).cents for p in closed]
    prev3_mtd = [
        queries.spending_total(conn, _same_days(p, today.day), today).cents for p in closed[-3:]
    ]
    avg_mtd = sum(prev3_mtd) // 3 if prev3_mtd else 0
    lines = [
        f"Mês atual (01 a {_ddmm(today)}): {r(total.cents)} em {total.count} gastos; "
        f"média dos 3 meses anteriores até o mesmo dia: {r(avg_mtd)}.",
        "Meses fechados: "
        + " · ".join(
            f"{MONTHS[p.start.month - 1]} {r(c)}" for p, c in zip(closed, totals, strict=True)
        )
        + ".",
    ]
    people = queries.spending_by(conn, month, today, "person")
    if len(people) > 1:
        lines.append(
            "Por pessoa no mês: " + " · ".join(f"{b.key} {r(b.cents)}" for b in people) + "."
        )
    return lines


def _categories(conn: sqlite3.Connection, today: date) -> list[str]:
    month = _month(today.year, today.month)
    now = {b.key: b.cents for b in queries.spending_by(conn, month, today, "category")}
    hist: dict[str, int] = defaultdict(int)
    for p in _closed_months(today, 3):
        for b in queries.spending_by(conn, p, today, "category"):
            hist[b.key] += b.cents
    keys = sorted(set(now) | set(hist), key=lambda k: -(max(now.get(k, 0), hist.get(k, 0) // 3)))
    rows = [
        f"{k} {r(now.get(k, 0))} (média {r(hist.get(k, 0) // 3)})" for k in keys[:MAX_CATEGORIES]
    ]
    head = f"Categorias (mês atual até {_ddmm(today)}; média de mês cheio nos 3 meses fechados): "
    return [head + "; ".join(rows) + "."]


def _board(conn: sqlite3.Connection, today: date) -> list[str]:
    lines = []
    bs = board.budgets(conn, today)
    if bs:
        lines.append(
            "Orçamentos: "
            + "; ".join(
                f"{sanitize_text(b.category)}{' (' + b.person + ')' if b.person else ''} "
                f"{r(b.spent_cents)} de {r(b.limit_cents)} ({round(b.pct)}%)"
                for b in bs
            )
            + "."
        )
    gs = board.goals(conn)
    if gs:
        lines.append(
            "Objetivos: "
            + "; ".join(
                f"{sanitize_text(g['title'], 40)} {r(g['saved_cents'])} de {r(g['target_cents'])}"
                + (f" até {g['deadline']}" if g["deadline"] else "")
                for g in gs
            )
            + "."
        )
    ns = board.notices(conn, today)
    if ns:
        lines.append(
            "Avisos no painel: "
            + "; ".join(sanitize_text(n["text"], 80) or "" for n in ns[:5])
            + "."
        )
    return lines


def _recurring(conn: sqlite3.Connection, today: date) -> list[str]:
    series = [s for s in recurring.detect(recurring.load_charges(conn, today), today) if s.active]
    if not series:
        return []
    total = sum(s.last_cents for s in series)
    items = [
        f"{(sanitize_text(s.label) or s.key)[:LABEL]} {r(s.last_cents)} (dia ~{s.last_date.day})"
        for s in series[:MAX_RECURRING]
    ]
    more = f" e mais {len(series) - MAX_RECURRING}" if len(series) > MAX_RECURRING else ""
    return [f"Recorrentes ativas ({r(total)}/mês): " + "; ".join(items) + more + "."]


def _cards(cards: list[dict[str, Any]]) -> list[str]:
    parts = []
    for c in cards:
        est = c["open_bill_estimate"]["estimated"]
        since = c["open_bill_estimate"]["closing_date_used"]
        if est:
            parts.append(
                f"{sanitize_text(c['account'])} ({c['owner']}): fatura aberta ~{r(est['cents'])}"
                + (f" desde {date.fromisoformat(since).strftime('%d/%m')}" if since else "")
            )
    return ["Cartões: " + "; ".join(parts) + "."] if parts else []


def _habits(conn: sqlite3.Connection, today: date) -> list[str]:
    lines = []
    since = (today - timedelta(days=180)).isoformat()
    # Income: per person and month, the day of the largest credit (salary-like).
    rows = conn.execute(
        f"SELECT {queries.PERSON_SQL} AS who, t.posted_date AS d, t.amount_cents AS c "  # noqa: S608
        f"FROM {queries.TX_FROM} WHERE t.amount_cents >= 50000 AND t.excluded_reason IS NULL "
        "AND t.posted_date >= ? AND t.posted_date <= ?",
        (since, today.isoformat()),
    ).fetchall()
    best: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        key = (sanitize_text(str(row["who"])) or "?", row["d"][:7])
        if key not in best or row["c"] > best[key][0]:
            best[key] = (row["c"], int(row["d"][8:10]))
    per_person: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for (who, _), v in best.items():
        per_person[who].append(v)
    incomes = [
        f"{who}: maior entrada do mês costuma cair por volta do dia "
        f"{round(statistics.median(d for _, d in v))} "
        f"(~{r(int(statistics.median(c for c, _ in v)))})"
        for who, v in sorted(per_person.items())
        if len(v) >= 3
    ]
    if incomes:
        lines.append("Entradas: " + "; ".join(incomes) + ".")
    # Eating out: orders per month, average ticket, busiest weekdays.
    cat = queries.category_sql()
    marks = ", ".join("?" * len(EATING_OUT))
    eat = conn.execute(
        f"SELECT t.posted_date AS d, -t.amount_cents AS c FROM {queries.TX_FROM} "  # noqa: S608
        f"WHERE t.amount_cents < 0 AND t.excluded_reason IS NULL AND {cat} IN ({marks}) "
        "AND t.posted_date >= ? AND t.posted_date <= ?",
        (*EATING_OUT, (today - timedelta(days=90)).isoformat(), today.isoformat()),
    ).fetchall()
    if len(eat) >= 6:
        days = Counter(date.fromisoformat(e["d"]).weekday() for e in eat).most_common(2)
        lines.append(
            f"Delivery/restaurantes (90 dias): ~{round(len(eat) / 3)} pedidos/mês, "
            f"ticket médio {r(sum(e['c'] for e in eat) // len(eat))}, "
            f"mais às {' e '.join(WEEKDAYS[d] for d, _ in days)}."
        )
    return lines


def kitchen_lines(status: dict[str, Any]) -> list[str]:
    """Diet side of the snapshot (from KitchenTools.kitchen_status)."""
    people = status.get("people") or []
    if not people:
        return ["Cozinha: dieta ainda não configurada (perfis, metas e teto semanal a combinar)."]
    parts = []
    for p in people:
        if p.get("missing"):
            parts.append(f"{p['person']}: falta {', '.join(p['missing'])}")
            continue
        line = f"{p['person']}: {p['kcal']} kcal/dia (porção {p['portion']}x), {p['weight_kg']} kg"
        if p.get("target_kg"):
            line += f" -> meta {p['target_kg']} kg"
        if p.get("trend_kg_week") is not None:
            line += f", tendência {p['trend_kg_week']:+} kg/sem"
        if p.get("restrictions"):
            line += f", restrições: {p['restrictions']}"
        parts.append(line)
    rules = status.get("rules") or {}
    week = status.get("this_week") or {}
    lines = ["Dieta: " + "; ".join(parts) + "."]
    lines.append(
        f"Cozinha na semana: {week.get('menu')}; comida {week.get('food_spent')}"
        + (f" de teto {rules['weekly_budget']}" if rules.get("weekly_budget") else "")
        + f"; {week.get('delivery_orders')} delivery(s)"
        + (
            f" (máx. {rules['delivery_max_week']})"
            if rules.get("delivery_max_week") is not None
            else ""
        )
        + f"; plano seguido {week.get('followed_plan')}."
    )
    if status.get("todo"):
        lines.append("A combinar na cozinha: " + "; ".join(status["todo"]) + ".")
    return lines


def build(conn: sqlite3.Connection, today: date, cards: list[dict[str, Any]]) -> str:
    last = conn.execute("SELECT MAX(finished_at) FROM sync_runs WHERE status = 'ok'").fetchone()[0]
    head = f"Retrato da casa em {today.strftime('%d/%m/%Y')}" + (
        f" (último sync {str(last)[:16].replace('T', ' ')} UTC)" if last else ""
    )
    sections = [
        _spending(conn, today),
        _categories(conn, today),
        _board(conn, today),
        _recurring(conn, today),
        _cards(cards),
        _habits(conn, today),
    ]
    body = [line for sec in sections for line in sec]
    notes = memory.list_notes(conn)
    if notes:
        body.append("O que a casa contou (memória):")
        body += [f"- [{n['id']}] {n['text']}" for n in notes[: memory.MAX_IN_CONTEXT]]
    body.append("Valores arredondados; use as ferramentas para valores exatos e lançamentos.")
    return head + "\n" + "\n".join(body)
