"""Energy targets and body metrics. Plain formulas, no model involved.

* BMR: Mifflin-St Jeor (kcal/day).
* TDEE: BMR x activity factor.
* Target: TDEE - 500 to lose (never below a floor), +300 to gain.
* Shared menu: dishes are written for a 2000 kcal reference; each person eats
  ``portion = target / 2000`` of every dish.

These are population estimates, not medical advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

REFERENCE_KCAL = 2000
ACTIVITY = {"sedentario": 1.2, "leve": 1.375, "moderado": 1.55, "intenso": 1.725}
FLOOR_KCAL = {"f": 1200, "m": 1500}
DEFICIT, SURPLUS = 500, 300


@dataclass(frozen=True, slots=True)
class Targets:
    bmr: int
    tdee: int
    kcal: int
    protein_g: int
    portion: float
    bmi: float


def age(birth_year: int, today: date) -> int:
    return today.year - birth_year


def bmr(sex: str, kg: float, height_cm: int, years: int) -> float:
    base = 10 * kg + 6.25 * height_cm - 5 * years
    return base + (5 if sex == "m" else -161)


def targets(
    sex: str, kg: float, height_cm: int, birth_year: int, activity: str, goal: str, today: date
) -> Targets:
    b = bmr(sex, kg, height_cm, age(birth_year, today))
    tdee = b * ACTIVITY.get(activity, ACTIVITY["sedentario"])
    if goal == "perder":
        kcal = max(tdee - DEFICIT, FLOOR_KCAL.get(sex, 1200), b * 0.9)
    elif goal == "ganhar":
        kcal = tdee + SURPLUS
    else:
        kcal = tdee
    protein = kg * (1.6 if goal in ("perder", "ganhar") else 1.2)
    portion = round(kcal / REFERENCE_KCAL / 0.05) * 0.05
    return Targets(
        bmr=round(b),
        tdee=round(tdee),
        kcal=round(kcal / 10) * 10,
        protein_g=round(protein),
        portion=round(portion, 2),
        bmi=round(kg / (height_cm / 100) ** 2, 1),
    )


def trend_kg_per_week(points: list[tuple[date, int]]) -> float | None:
    """Least-squares slope of the last weigh-ins (grams by day) in kg/week."""
    pts = sorted(points)[-6:]
    if len(pts) < 2 or (pts[-1][0] - pts[0][0]).days < 6:
        return None
    xs = [(d - pts[0][0]).days for d, _ in pts]
    ys = [g / 1000 for _, g in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    slope_day = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den
    return round(slope_day * 7, 2)


def eta(current_g: int, target_g: int, kg_per_week: float | None, today: date) -> date | None:
    """When the target would be reached at the current trend (None if moving away/flat)."""
    if kg_per_week is None or not kg_per_week:
        return None
    weeks = (target_g - current_g) / 1000 / kg_per_week
    if weeks <= 0 or weeks > 156:
        return None
    return today + timedelta(days=round(weeks * 7))
