"""Kitchen data: diet profiles, household rules, weigh-ins, daily check-ins and the
weekly shared menu. Validation lives here; every message is safe to show."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any

from grana_mcp.privacy.redact import sanitize_text
from grana_mcp.store.db import now_utc

MEALS = ("cafe", "almoco", "lanche", "jantar")
UNITS = {
    "g": ("kg", 0.001),
    "kg": ("kg", 1.0),
    "ml": ("l", 0.001),
    "l": ("l", 1.0),
    "un": ("un", 1.0),
}
MAX_INGREDIENTS = 15
MAX_MEALS = 28
_SEARCH_RE = re.compile(r"[^a-z0-9 ]+")


class KitchenError(ValueError):
    """Invalid kitchen input (message is safe to show)."""


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def person_id(conn: sqlite3.Connection, ref: str) -> str:
    row = conn.execute(
        "SELECT id FROM people WHERE id = lower(?) OR lower(display_name) = lower(?)",
        (ref.strip(), ref.strip()),
    ).fetchone()
    if not row:
        names = ", ".join(r[0] for r in conn.execute("SELECT display_name FROM people"))
        raise KitchenError(f"pessoa '{sanitize_text(ref, 32)}' não encontrada (pessoas: {names})")
    return str(row[0])


def _text(value: str | None, limit: int = 200) -> str | None:
    return sanitize_text(value, limit) if value else None


def _day(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise KitchenError(f"{field} deve ser AAAA-MM-DD") from None


# --- profiles and rules ---

PROFILE_FIELDS = (
    "sex",
    "birth_year",
    "height_cm",
    "activity",
    "goal",
    "target_g",
    "deadline",
    "restrictions",
    "likes",
    "dislikes",
)


def set_profile(conn: sqlite3.Connection, person: str, by: str | None, **fields: Any) -> str:
    pid = person_id(conn, person)
    clean: dict[str, Any] = {}
    if fields.get("sex") is not None:
        sex = str(fields["sex"]).strip().lower()[:1]
        if sex not in ("f", "m"):
            raise KitchenError("sexo: 'f' ou 'm' (usado só na fórmula do metabolismo)")
        clean["sex"] = sex
    for key, lo, hi in (("birth_year", 1900, 2100), ("height_cm", 100, 250)):
        if fields.get(key) is not None:
            v = int(fields[key])
            if not lo <= v <= hi:
                raise KitchenError(f"{key} fora do intervalo")
            clean[key] = v
    if fields.get("activity") is not None:
        a = str(fields["activity"]).strip().lower().replace("á", "a")
        if a not in ("sedentario", "leve", "moderado", "intenso"):
            raise KitchenError("atividade: sedentario, leve, moderado ou intenso")
        clean["activity"] = a
    if fields.get("goal") is not None:
        g = str(fields["goal"]).strip().lower()
        if g not in ("perder", "manter", "ganhar"):
            raise KitchenError("objetivo: perder, manter ou ganhar")
        clean["goal"] = g
    if fields.get("target_kg") is not None:
        grams = round(float(fields["target_kg"]) * 1000)
        if not 30000 <= grams <= 300000:
            raise KitchenError("peso-alvo fora do intervalo")
        clean["target_g"] = grams
    if fields.get("deadline") is not None:
        clean["deadline"] = _day(fields["deadline"], "prazo")
    for key in ("restrictions", "likes", "dislikes"):
        if fields.get(key) is not None:
            clean[key] = _text(fields[key])
    if not clean:
        raise KitchenError("nada para atualizar")
    conn.execute(
        "INSERT INTO diet_people (person_id, updated_at, updated_by) VALUES (?, ?, ?) "
        "ON CONFLICT (person_id) DO NOTHING",
        (pid, now_utc(), by),
    )
    sets = ", ".join(f"{k} = ?" for k in clean)
    conn.execute(
        f"UPDATE diet_people SET {sets}, updated_at = ?, updated_by = ? WHERE person_id = ?",  # noqa: S608
        (*clean.values(), now_utc(), by, pid),
    )
    conn.commit()
    return f"Perfil de {pid} atualizado: {', '.join(clean)}."


def profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT d.*, p.display_name AS name FROM diet_people d JOIN people p ON p.id = d.person_id "
        "ORDER BY p.display_name"
    ).fetchall()


def set_rules(conn: sqlite3.Connection, by: str | None, **fields: Any) -> str:
    clean: dict[str, Any] = {}
    if fields.get("weekly_budget") is not None:
        cents = round(float(fields["weekly_budget"]) * 100)
        if not 0 <= cents <= 100_000_00:
            raise KitchenError("orçamento semanal fora do intervalo")
        clean["weekly_budget_cents"] = cents
    if fields.get("delivery_max_week") is not None:
        n = int(fields["delivery_max_week"])
        if not 0 <= n <= 50:
            raise KitchenError("delivery por semana fora do intervalo")
        clean["delivery_max_week"] = n
    if fields.get("notes") is not None:
        clean["notes"] = _text(fields["notes"], 300)
    if fields.get("home_geohash") is not None:
        gh = str(fields["home_geohash"]).strip().lower()[:6]
        if not re.fullmatch(r"[0-9b-hjkmnp-z]{5,6}", gh):
            raise KitchenError("localização inválida")
        clean["home_geohash"] = gh
    if fields.get("radius_km") is not None:
        r = int(fields["radius_km"])
        if not 1 <= r <= 20:
            raise KitchenError("raio entre 1 e 20 km")
        clean["radius_km"] = r
    if not clean:
        raise KitchenError("nada para atualizar")
    conn.execute(
        "INSERT INTO diet_rules (id, updated_at, updated_by) VALUES (1, ?, ?) "
        "ON CONFLICT (id) DO NOTHING",
        (now_utc(), by),
    )
    sets = ", ".join(f"{k} = ?" for k in clean)
    conn.execute(
        f"UPDATE diet_rules SET {sets}, updated_at = ?, updated_by = ? WHERE id = 1",  # noqa: S608
        (*clean.values(), now_utc(), by),
    )
    conn.commit()
    return "Regras da cozinha atualizadas: " + ", ".join(clean) + "."


def rules(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row: sqlite3.Row | None = conn.execute("SELECT * FROM diet_rules WHERE id = 1").fetchone()
    return row


# --- logs ---


def log_weight(conn: sqlite3.Connection, person: str, kg: float, day: date, by: str | None) -> str:
    pid = person_id(conn, person)
    grams = round(float(kg) * 1000)
    if not 30000 <= grams <= 300000:
        raise KitchenError("peso fora do intervalo")
    conn.execute(
        "INSERT INTO weight_log (person_id, day, grams, by) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (person_id, day) DO UPDATE SET grams = excluded.grams, by = excluded.by",
        (pid, day.isoformat(), grams, by),
    )
    conn.commit()
    return f"Peso de {pid} em {day.strftime('%d/%m')}: {grams / 1000:.1f} kg."


def weights(conn: sqlite3.Connection, since: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT w.person_id, p.display_name AS name, w.day, w.grams FROM weight_log w "
        "JOIN people p ON p.id = w.person_id WHERE w.day >= ? ORDER BY w.day",
        (since.isoformat(),),
    ).fetchall()


def log_day(
    conn: sqlite3.Connection, followed: bool, day: date, note: str | None, by: str | None
) -> str:
    conn.execute(
        "INSERT INTO day_log (day, followed, note, by) VALUES (?, ?, ?, ?) ON CONFLICT (day) "
        "DO UPDATE SET followed = excluded.followed, note = excluded.note, by = excluded.by",
        (day.isoformat(), int(bool(followed)), _text(note, 140), by),
    )
    conn.commit()
    return f"{day.strftime('%d/%m')}: {'seguiram' if followed else 'não seguiram'} o plano."


def days(conn: sqlite3.Connection, since: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT day, followed, note, by FROM day_log WHERE day >= ? ORDER BY day",
        (since.isoformat(),),
    ).fetchall()


# --- weekly menu ---


_STOPWORDS = {"de", "da", "do", "das", "dos", "com", "e", "tipo", "pct", "pacote", "kg", "g", "ml",
              "l", "un", "unidade", "unidades", "fresco", "fresca"}  # fmt: skip


def search_key(text: str) -> str:
    """Short, generic search term: receipts are abbreviated, so 'arroz tipo 1 5kg' -> 'arroz'.

    Sizes and filler words go away (package sizes are read from each receipt line);
    at most 2 words are kept.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = [
        w
        for w in _SEARCH_RE.sub(" ", folded).split()
        if w not in _STOPWORDS and not any(ch.isdigit() for ch in w)
    ]
    return " ".join(words[:2])[:40]


@dataclass(frozen=True, slots=True)
class Ingredient:
    item: str
    qty: float  # per base portion, in ``unit``
    unit: str
    search: str
    have: bool = False  # already at home: in the menu, not in the shopping list

    @property
    def base(self) -> tuple[str, float]:
        """(base unit, quantity in base unit) per base portion."""
        unit, factor = UNITS[self.unit]
        return unit, self.qty * factor


def parse_ingredients(raw: Any) -> list[Ingredient]:
    """Ingredients of one meal; an empty list is fine (delivery, leftovers, eating out)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise KitchenError("ingredientes: lista com item, qty, unit (g, kg, ml, l, un)")
    out = []
    for i in raw[:MAX_INGREDIENTS]:
        if not isinstance(i, dict):
            raise KitchenError("cada ingrediente é um objeto {item, qty, unit, search}")
        item = sanitize_text(str(i.get("item", "")), 60)
        unit = str(i.get("unit", "")).strip().lower()
        try:
            qty = float(i.get("qty", 0))
        except (TypeError, ValueError):
            raise KitchenError(f"quantidade inválida em '{item}'") from None
        if not item or unit not in UNITS or not 0 < qty <= 5000:
            raise KitchenError(f"ingrediente inválido: '{item}' {qty} {unit}")
        search = search_key(str(i.get("search") or item))
        have = i.get("have") is True
        out.append(Ingredient(item, qty, unit, search or search_key(item), have))
    return out


def set_week(conn: sqlite3.Connection, week_start: date, meals: Any, by: str | None) -> str:
    """Replace the menu of the week starting at ``week_start`` (a Monday)."""
    if week_start.weekday() != 0:
        raise KitchenError("week_start deve ser uma segunda-feira")
    if not isinstance(meals, list) or not meals:
        raise KitchenError("meals: lista de refeições")
    rows = []
    seen = set()
    for m in meals[:MAX_MEALS]:
        if not isinstance(m, dict):
            raise KitchenError("cada refeição é um objeto")
        try:
            day = int(m.get("day", -1))
        except (TypeError, ValueError):
            day = -1
        meal = str(m.get("meal", "")).strip().lower().replace("é", "e").replace("ç", "c")
        if not 0 <= day <= 6 or meal not in MEALS:
            raise KitchenError("day 0 (segunda) a 6 (domingo); meal: cafe, almoco, lanche, jantar")
        if (day, meal) in seen:
            raise KitchenError(f"refeição repetida: dia {day} {meal}")
        seen.add((day, meal))
        title = sanitize_text(str(m.get("title", "")), 80)
        if not title:
            raise KitchenError("cada refeição precisa de um título")
        ingredients = parse_ingredients(m.get("ingredients"))
        kcal = m.get("kcal")
        protein = m.get("protein_g")
        rows.append(
            (
                week_start.isoformat(),
                day,
                meal,
                title,
                _text(m.get("how"), 300),
                int(kcal) if kcal is not None else None,
                int(protein) if protein is not None else None,
                json.dumps([asdict(i) for i in ingredients], ensure_ascii=False),
                now_utc(),
                by,
            )
        )
    conn.execute("DELETE FROM meal_plan WHERE week_start = ?", (week_start.isoformat(),))
    try:
        conn.executemany(
            "INSERT INTO meal_plan (week_start, day, meal, title, how, kcal, protein_g, "
            "ingredients, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise KitchenError(f"refeição inválida ({exc})") from None
    conn.commit()
    return f"Cardápio da semana de {week_start.strftime('%d/%m')} salvo: {len(rows)} refeição(ões)."


def week(conn: sqlite3.Connection, week_start: date) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM meal_plan WHERE week_start = ? ORDER BY day, "
        "CASE meal WHEN 'cafe' THEN 0 WHEN 'almoco' THEN 1 WHEN 'lanche' THEN 2 ELSE 3 END",
        (week_start.isoformat(),),
    ).fetchall()


def ingredients_of(row: sqlite3.Row) -> list[Ingredient]:
    return [Ingredient(**i) for i in json.loads(row["ingredients"])]


@dataclass
class Need:
    item: str
    search: str
    unit: str  # base unit
    qty: float  # total, in base unit
    meals: int


def shopping_needs(meals: list[sqlite3.Row], portions: float) -> list[Need]:
    """Total quantity of each ingredient for the week, for everyone (portions summed)."""
    needs: dict[tuple[str, str], Need] = {}
    for m in meals:
        for ing in ingredients_of(m):
            if ing.have:
                continue
            unit, qty = ing.base
            key = (ing.search, unit)
            n = needs.setdefault(key, Need(ing.item, ing.search, unit, 0.0, 0))
            n.qty += qty * portions
            n.meals += 1
    return sorted(needs.values(), key=lambda n: n.item.lower())
