"""Which phone notifications are due, from the local SQLite only.

``collect`` is pure (database + clock in, alerts out); ``run`` dedupes them
against ``notify_sent``, respects quiet hours and hands them to a sender.
Every alert has a stable ``key`` so it fires once. On the very first run every
current alert is recorded as seen without sending, so turning notifications
on does not replay the past.

Texts are short and carry no account numbers; merchant names are sanitized
and third-party people masked, the same as in ``find_transactions``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from grana_mcp import board
from grana_mcp.analytics import queries
from grana_mcp.analytics.periods import Period, month_bounds, shift_month
from grana_mcp.money import format_brl
from grana_mcp.pipeline import recurring
from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

BOOTSTRAP_KEY = "_bootstrap"
DEFAULT_BIG_PURCHASE = 500_00
QUIET_START, QUIET_END = 22, 7  # local hours; only "urgent" alerts go out in between
WEEKLY_HOUR = 19  # Sunday summary after this hour
MAX_PER_RUN = 5
RECENT_DAYS = 3


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    title: str
    body: str
    url: str = "/"
    urgent: bool = False
    tag: str = ""
    group: str = ""  # "buy": merged into one "N gastos novos" digest per run
    cents: int = 0
    label: str = ""


@dataclass
class Settings:
    big_purchase_cents: int = DEFAULT_BIG_PURCHASE
    weekly: bool = True
    purchases: bool = True  # digest of every new spending row (below the big threshold)


@dataclass
class RunResult:
    sent: list[Alert] = field(default_factory=list)
    deferred: int = 0
    bootstrapped: int = 0
    deliveries: int = 0
    failures: int = 0


def _brl(cents: int) -> str:
    return format_brl(abs(cents))


def _short(d: date) -> str:
    return d.strftime("%d/%m")


# --- individual alert families ---


def budget_alerts(conn: sqlite3.Connection, today: date) -> list[Alert]:
    out = []
    month = today.strftime("%Y-%m")
    for b in board.budgets(conn, today):
        who = f" ({b.person})" if b.person else ""
        base = f"budget:{month}:{b.category.lower()}:{(b.person or '').lower()}"
        name = sanitize_text(b.category) or "?"
        if b.pct >= 100:
            out.append(
                Alert(
                    f"{base}:100",
                    f"Orçamento de {name}{who} estourou",
                    f"{_brl(b.spent_cents)} de {_brl(b.limit_cents)} "
                    f"(passou {_brl(b.spent_cents - b.limit_cents)}).",
                    tag=f"budget-{name}",
                )
            )
        elif b.pct >= 80:
            out.append(
                Alert(
                    f"{base}:80",
                    f"{name}{who}: {round(b.pct)}% do orçamento",
                    f"{_brl(b.spent_cents)} de {_brl(b.limit_cents)}; "
                    f"sobram {_brl(b.limit_cents - b.spent_cents)} até o fim do mês.",
                    tag=f"budget-{name}",
                )
            )
    return out


def bill_alerts(cards: list[dict[str, Any]], today: date) -> list[Alert]:
    """``cards`` is ``GranaTools.card_bills()['cards']``."""
    out = []
    for c in cards:
        acct = sanitize_text(c["account"]) or "cartão"
        owner = f" ({c['owner']})" if c.get("owner") else ""
        latest = c["closed_bills"][0] if c["closed_bills"] else None
        if latest:
            due = date.fromisoformat(latest["due_date"])
            left = latest["total"]["cents"] - latest["paid"]["cents"]
            if left > 0 and 0 <= (due - today).days <= 3:
                when = "hoje" if due == today else f"em {_short(due)}"
                out.append(
                    Alert(
                        f"bill-due:{c['account_id']}:{due.isoformat()}",
                        f"Fatura {acct}{owner} vence {when}",
                        f"Faltam {_brl(left)} de {_brl(latest['total']['cents'])}.",
                        urgent=due == today,
                        tag=f"bill-{c['account_id']}",
                    )
                )
        est = c["open_bill_estimate"]
        closing = est.get("closing_date_used")
        if closing and est.get("estimated"):
            last = date.fromisoformat(closing)
            y, m = shift_month(last.year, last.month, 1)
            nxt = date(y, m, min(last.day, month_bounds(y, m)[1].day))
            while nxt < today:
                y, m = shift_month(y, m, 1)
                nxt = date(y, m, min(last.day, month_bounds(y, m)[1].day))
            if 0 <= (nxt - today).days <= 3:
                out.append(
                    Alert(
                        f"bill-close:{c['account_id']}:{nxt.isoformat()}",
                        f"Fatura {acct}{owner} fecha ~{_short(nxt)}",
                        f"Estimativa até agora: {est['estimated']['brl']}.",
                        tag=f"bill-{c['account_id']}",
                    )
                )
    return out


def purchase_alerts(
    conn: sqlite3.Connection, today: date, threshold: int, every: bool = True
) -> list[Alert]:
    """Big spending -> one alert each; the rest (if ``every``) -> "buy" items for a digest."""
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    household = queries.household_hashes(conn)
    rows = conn.execute(
        f"SELECT t.id, t.posted_date, t.amount_cents, t.description_raw, t.merchant, "  # noqa: S608
        "t.description_clean, "
        f"t.counterpart_doc_hash, t.counterpart_doc_type, t.operation_type, "
        f"{queries.PERSON_SQL} AS person, {queries.category_sql()} AS cat "
        f"FROM {queries.TX_FROM} WHERE t.amount_cents <= ? AND t.excluded_reason IS NULL "
        "AND t.posted_date >= ? AND t.posted_date <= ? "
        "AND COALESCE(t.installment_number, 1) = 1 ORDER BY t.posted_date, t.id",
        (-threshold if not every else -1, since, today.isoformat()),
    ).fetchall()
    out = []
    for r in rows:
        label = queries.merchant_label(r, household)[:40]
        cat = sanitize_text(r["cat"]) or "Outros"
        if -r["amount_cents"] < threshold:
            out.append(
                Alert(
                    f"buy:{r['id']}",
                    f"Gasto: {_brl(r['amount_cents'])}",
                    f"{label} · {cat} · {sanitize_text(str(r['person']))}",
                    url=f"/#cat={quote(cat)}",
                    group="buy",
                    cents=-r["amount_cents"],
                    label=label,
                )
            )
            continue
        out.append(
            Alert(
                f"tx:{r['id']}",
                f"Gasto de {_brl(r['amount_cents'])}",
                f"{label} · {cat} · {sanitize_text(str(r['person']))} "
                f"· {_short(date.fromisoformat(r['posted_date']))}",
                url=f"/#cat={quote(cat)}",
                tag=f"tx-{r['id']}",
            )
        )
    return out


def recurring_alerts(conn: sqlite3.Connection, today: date) -> list[Alert]:
    out = []
    recent = today - timedelta(days=7)
    for s in recurring.detect(recurring.load_charges(conn, today), today):
        if not s.active or s.last_date < recent:
            continue
        name = sanitize_text(s.label) or s.key
        if s.price_changed and s.previous_cents is not None:
            up = s.last_cents > s.previous_cents
            out.append(
                Alert(
                    f"rec-price:{s.key}:{s.last_date.isoformat()}",
                    f"{name} {'subiu' if up else 'baixou'} de preço",
                    f"{_brl(s.previous_cents)} → {_brl(s.last_cents)} por mês.",
                    tag=f"rec-{s.key}",
                )
            )
        elif s.occurrences <= recurring.MIN_OCCURRENCES:  # just became recurring
            out.append(
                Alert(
                    f"rec-new:{s.key}",
                    f"Cobrança recorrente: {name}",
                    f"{_brl(s.last_cents)} por mês ({s.occurrences} vezes seguidas, "
                    f"{sanitize_text(s.category)}).",
                    tag=f"rec-{s.key}",
                )
            )
    return out


def weekly_alert(conn: sqlite3.Connection, now: datetime) -> list[Alert]:
    today = now.date()
    if today.weekday() != 6 or now.hour < WEEKLY_HOUR:
        return []
    start = today - timedelta(days=6)
    week = queries.spending_total(conn, Period(start, today, "semana"), today)
    past = [
        queries.spending_total(
            conn, Period(start - timedelta(days=7 * k), today - timedelta(days=7 * k), "s"), today
        ).cents
        for k in range(1, 9)
    ]
    avg = sum(past) // len(past)
    top = queries.spending_by(conn, Period(start, today, "semana"), today, "category")[:2]
    cmp = ""
    if avg:
        diff = round(100 * (week.cents - avg) / avg)
        cmp = f" ({'+' if diff > 0 else ''}{diff}% vs. média de 8 semanas)"
    tops = ", ".join(f"{b.key} {_brl(b.cents)}" for b in top)
    year, wk, _ = today.isocalendar()
    return [
        Alert(
            f"week:{year}-{wk:02d}",
            f"Semana: {_brl(week.cents)}{cmp}",
            (f"Maiores: {tops}." if tops else "Sem gastos na semana.") + " Toque para ver o mês.",
            tag="week",
        )
    ]


def health_alerts(conn: sqlite3.Connection, now: datetime) -> list[Alert]:
    out = []
    today = now.date()
    last_ok = conn.execute("SELECT MAX(finished_at) FROM sync_runs WHERE status = 'ok'").fetchone()[
        0
    ]
    if last_ok:
        age = now.astimezone() - datetime.fromisoformat(last_ok).astimezone()
        if age > timedelta(hours=36):
            out.append(
                Alert(
                    f"sync-stale:{today.isoformat()}",
                    "O grana não atualiza há mais de 1 dia",
                    f"Último sync ok {age.days or 1} dia(s) atrás. Rode 'grana-mcp doctor' no PC.",
                    urgent=False,
                    tag="sync",
                )
            )
    for run in conn.execute(
        "SELECT r.id, r.profile_id FROM sync_runs r WHERE r.status = 'error' AND r.id = "
        "(SELECT MAX(id) FROM sync_runs x WHERE x.profile_id = r.profile_id)"
    ):
        out.append(
            Alert(
                f"sync-error:{run['id']}",
                "O sync do grana falhou",
                f"Perfil {sanitize_text(run['profile_id'])}. "
                "Rode 'grana-mcp sync' no PC para ver o erro.",
                tag="sync",
            )
        )
    for item in conn.execute(
        "SELECT i.id, i.consent_expires_at, COALESCE(p.display_name, i.profile_id) AS who "
        "FROM items i LEFT JOIN profiles p ON p.id = i.profile_id "
        "WHERE i.consent_expires_at IS NOT NULL"
    ):
        expires = datetime.fromisoformat(item["consent_expires_at"]).date()
        days = (expires - today).days
        for limit in (7, 30):
            if 0 <= days <= limit:
                out.append(
                    Alert(
                        f"consent:{item['id']}:{expires.isoformat()}:{limit}",
                        "Autorização do banco perto de vencer",
                        f"{sanitize_text(item['who'])}: vence em {_short(expires)} ({days} dias). "
                        "Renove no app Meu Pluggy.",
                        tag=f"consent-{item['id']}",
                    )
                )
                break
    return out


def notice_alerts(conn: sqlite3.Connection, today: date) -> list[Alert]:
    out = []
    for n in board.notices(conn, today):
        created = str(n["created_at"])[:10]
        if created < (today - timedelta(days=2)).isoformat():
            continue
        by = f" (por {n['created_by']})" if n["created_by"] else ""
        out.append(
            Alert(
                f"notice:{n['id']}",
                f"Aviso no painel{by}",
                sanitize_text(n["text"], board.MAX_NOTICE) or "",
                tag=f"notice-{n['id']}",
            )
        )
    return out


def collect(
    conn: sqlite3.Connection,
    now: datetime,
    cards: list[dict[str, Any]],
    settings: Settings | None = None,
) -> list[Alert]:
    s = settings or Settings()
    today = now.date()
    alerts = [
        *health_alerts(conn, now),
        *bill_alerts(cards, today),
        *budget_alerts(conn, today),
        *notice_alerts(conn, today),
        *purchase_alerts(conn, today, s.big_purchase_cents, s.purchases),
        *recurring_alerts(conn, today),
    ]
    if s.weekly:
        alerts += weekly_alert(conn, now)
    # a budget past 100% makes its 80% alert moot
    over = {a.key[:-4] for a in alerts if a.key.startswith("budget:") and a.key.endswith(":100")}
    return [a for a in alerts if not (a.key.endswith(":80") and a.key[:-3] in over)]


# --- delivery ---


def _sent(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM notify_sent")}


def _mark(conn: sqlite3.Connection, keys: list[str]) -> None:
    ts = now_utc()
    conn.executemany(
        "INSERT OR IGNORE INTO notify_sent (key, sent_at) VALUES (?, ?)", [(k, ts) for k in keys]
    )
    conn.commit()


def quiet(now: datetime) -> bool:
    return now.hour >= QUIET_START or now.hour < QUIET_END


Deliver = Callable[[Alert], tuple[int, int]]  # -> (delivered, failed)


def run(
    conn: sqlite3.Connection,
    now: datetime,
    cards: list[dict[str, Any]],
    deliver: Deliver,
    settings: Settings | None = None,
) -> RunResult:
    result = RunResult()
    alerts = collect(conn, now, cards, settings)
    seen = _sent(conn)
    if BOOTSTRAP_KEY not in seen:
        # also the 80% keys of budgets already past 100%, so they never fire later
        extra = [a.key[:-4] + ":80" for a in alerts if a.key.endswith(":100")]
        _mark(conn, [BOOTSTRAP_KEY, *(a.key for a in alerts), *extra])
        result.bootstrapped = len(alerts)
        return result
    due = [a for a in alerts if a.key not in seen]
    covered: list[str] = []
    buys = [a for a in due if a.group == "buy"]
    if len(buys) > 1:
        due = [a for a in due if a.group != "buy"]
        due.append(digest(buys))
        covered = [a.key for a in buys]
    if quiet(now):
        result.deferred = sum(1 for a in due if not a.urgent)
        due = [a for a in due if a.urgent]
        covered = []
    if not due:
        return result
    batch = due[:MAX_PER_RUN]
    if len(due) > MAX_PER_RUN:
        rest = len(due) - MAX_PER_RUN + 1
        more = Alert(f"_more:{now.isoformat()}", f"+{rest} avisos no grana", "Abra o painel.")
        batch = [*due[: MAX_PER_RUN - 1], more]
    for alert in batch:
        ok, bad = deliver(alert)
        result.deliveries += ok
        result.failures += bad
        result.sent.append(alert)
    _mark(conn, [*(a.key for a in due), *covered])
    return result


def digest(buys: list[Alert]) -> Alert:
    """One notification for several new spending rows: count, total and the largest."""
    total = sum(a.cents for a in buys)
    top = sorted(buys, key=lambda a: -a.cents)
    body = " · ".join(f"{a.label} {_brl(a.cents)}" for a in top[:3])
    if len(top) > 3:
        body += f" e mais {len(top) - 3}"
    return Alert(
        "buys:" + ",".join(sorted(a.key[4:] for a in buys))[:200],
        f"{len(buys)} gastos novos · {_brl(total)}",
        body + ".",
        tag="buys",
    )


def db_cards(db_path: Path, today: date) -> list[dict[str, Any]]:
    from grana_mcp.server import GranaTools

    cards: list[dict[str, Any]] = GranaTools(db_path, lambda: today).card_bills()["cards"]
    return cards
