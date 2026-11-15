from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from grana_mcp import config
from grana_mcp.kitchen import nutrition, prices, store
from grana_mcp.kitchen.service import KitchenTools
from grana_mcp.server import build_server
from grana_mcp.store import db

TODAY = date(2026, 11, 18)  # a Wednesday
MONDAY = "2026-11-16"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(config.db_path())
    c.executescript(
        "INSERT INTO people (id, display_name, created_at) VALUES "
        "('ana', 'Ana', 'x'), ('bia', 'Bia', 'x');"
    )
    yield c
    c.close()


def store_item(
    desc: str,
    cents: int,
    market: str,
    *,
    gtin: str = "789",
    sold: str = "2026-11-15T10:00:00Z",
    dist: float = 1.0,
) -> dict[str, Any]:
    return {
        "desc": desc, "valor": f"{cents / 100:.2f}", "gtin": gtin, "datahora": sold,
        "distkm": str(dist),
        "estabelecimento": {"codigo": market, "nm_fan": f"MERCADO {market}", "nm_logr": "RUA X",
                            "nr_logr": "1", "bairro": "CENTRO"},
    }  # fmt: skip


CATALOG: dict[str, list[dict[str, Any]]] = {
    "arroz": [
        store_item("ARROZ TIPO 1 5KG", 25_00, "A"),
        store_item("ARROZ TIPO 1 1KG", 7_00, "B"),
        store_item("ARROZ TIPO 1 5KG", 30_00, "B"),
    ],
    "feijao": [
        store_item("FEIJAO PRETO 1KG", 8_00, "A"),
        store_item("FEIJAO PRETO 1KG", 6_00, "B"),
    ],
    "ovos": [store_item("OVOS BRANCOS DZ", 12_00, "A"), store_item("OVOS 30UN", 20_00, "B")],
    "banana": [store_item("BANANA NANICA KG", 5_00, "A", gtin="")],
}


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, term: str, geohash: str, radius_km: int) -> list[dict[str, Any]]:
        self.calls.append(term)
        return CATALOG.get(term, [])


OSM = [
    {"type": "node", "id": 1, "lat": -25.4290, "lon": -49.2740,
     "tags": {"shop": "supermarket", "name": "Zamprogna", "addr:street": "Rua X",
              "addr:housenumber": "10"}},
    {"type": "way", "id": 2, "center": {"lat": -25.4300, "lon": -49.2700},
     "tags": {"shop": "greengrocer", "name": "Sacolão da Vila"}},
    {"type": "node", "id": 3, "lat": -25.4284, "lon": -49.2733,
     "tags": {"shop": "clothes", "name": "Loja de Roupa"}},  # not food
    {"type": "node", "id": 4, "lat": -25.4284, "lon": -49.2733, "tags": {"shop": "butcher"}},
]  # fmt: skip


class FakeShops:
    def __init__(self) -> None:
        self.calls: list[tuple[float, float, int]] = []

    def __call__(self, lat: float, lon: float, radius_m: int) -> list[dict[str, Any]]:
        self.calls.append((lat, lon, radius_m))
        return OSM


def kt(fetch: FakeSource | None = None, actor: str | None = "ana") -> KitchenTools:
    return KitchenTools(config.db_path(), lambda: TODAY, lambda: actor, fetch, FakeShops())


# --- formulas ---


def test_targets_and_trend() -> None:
    t = nutrition.targets("f", 70, 165, 1994, "leve", "perder", TODAY)
    assert t.bmr == 1410  # 10*70 + 6.25*165 - 5*32 - 161
    assert t.tdee == 1939 and t.kcal == 1440 and t.portion == 0.7
    assert t.protein_g == 112 and t.bmi == 25.7
    low = nutrition.targets("f", 50, 155, 1990, "sedentario", "perder", TODAY)
    assert low.kcal >= 1200  # never below the floor
    assert nutrition.targets("m", 80, 180, 1990, "moderado", "manter", TODAY).portion == 1.35
    pts = [(date(2026, 10, 1), 80000), (date(2026, 10, 8), 79500), (date(2026, 10, 15), 79000)]
    assert nutrition.trend_kg_per_week(pts) == -0.5
    assert nutrition.trend_kg_per_week(pts[:1]) is None
    assert nutrition.eta(79000, 75000, -0.5, TODAY) == date(2027, 1, 13)
    assert nutrition.eta(79000, 75000, 0.2, TODAY) is None  # moving away


@pytest.mark.parametrize(
    ("desc", "expected"),
    [
        ("ARROZ TIPO 1 5KG", (5.0, "kg")),
        ("CAFE 500G", (0.5, "kg")),
        ("LEITE UHT 1L", (1.0, "l")),
        ("REFRI 2X350ML", (0.7, "l")),
        ("AZEITE 0,5L", (0.5, "l")),
        ("OVOS BRANCOS DZ", (12.0, "un")),
        ("OVOS 30UN", (30.0, "un")),
        ("OVOS GRACIANA BCO EXTRA C10", (10.0, "un")),
        ("OVOS VERMELHOS C/20", (20.0, "un")),
        ("BANANA NANICA KG", (1.0, "kg")),
        ("BANANA NANICA", None),
    ],
)
def test_parse_size(desc: str, expected: tuple[float, str] | None) -> None:
    got = prices.parse_size(desc)
    assert got is None if expected is None else (got is not None and got == pytest.approx(expected))


# --- profiles, rules, logs ---


def test_profiles_rules_and_logs(conn: sqlite3.Connection) -> None:
    k = kt()
    k.set_diet_profile("Ana", sex="F", birth_year=1994, height_cm=165, activity="leve",
                       goal="perder", target_kg=65, restrictions="sem lactose")  # fmt: skip
    with pytest.raises(store.KitchenError):
        k.set_diet_profile("Carla", goal="perder")
    with pytest.raises(store.KitchenError):
        k.set_diet_profile("ana", activity="maratonista")
    status = k.kitchen_status()
    (ana,) = status["people"]
    assert ana["missing"] == ["peso"] and "kcal" not in ana
    k.log_weight("ana", 70, "2026-11-04")
    k.log_weight("ana", 69.4, "2026-11-11")
    k.log_weight("ana", 69, "2026-11-18")
    (ana,) = k.kitchen_status()["people"]
    assert ana["kcal"] == 1430 and ana["weight_kg"] == 69.0 and ana["trend_kg_week"] == -0.5
    assert ana["eta"] == "2027-01-13"
    assert any("com comida por semana" in t for t in k.kitchen_status()["todo"])
    k.set_kitchen_rules(weekly_budget=450, delivery_max_week=2, home_geohash="6gkzqf")
    with pytest.raises(store.KitchenError):
        k.set_kitchen_rules(home_geohash="<script>")
    k.log_day(True)
    k.log_day(False, "pizza")  # last answer of the day wins
    rows = store.days(conn, TODAY)
    assert [(r["followed"], r["note"], r["by"]) for r in rows] == [(0, "pizza", "ana")]
    assert k.kitchen_status()["rules"]["weekly_budget"] == "R$ 450,00"


# --- menu and shopping ---

MEALS = [
    {
        "day": 0, "meal": "almoco", "title": "Arroz, feijão e ovo", "kcal": 650, "protein_g": 30,
        "ingredients": [
            {"item": "arroz", "qty": 80, "unit": "g", "search": "arroz"},
            {"item": "feijão", "qty": 60, "unit": "g", "search": "feijão"},
            {"item": "ovo", "qty": 2, "unit": "un", "search": "ovos"},
        ],
    },
    {
        "day": 1, "meal": "almoço", "title": "Arroz e ovo com banana",
        "ingredients": [
            {"item": "arroz", "qty": 80, "unit": "g", "search": "arroz"},
            {"item": "ovo", "qty": 2, "unit": "un", "search": "ovos"},
            {"item": "banana", "qty": 120, "unit": "g", "search": "banana"},
        ],
    },
]  # fmt: skip


def test_menu_validation(conn: sqlite3.Connection) -> None:
    k = kt()
    assert k.set_meal_plan(MEALS, "2026-11-17")["ok"]  # any day -> that week's Monday
    assert k.meal_plan("2026-11-19")["week_start"] == MONDAY
    with pytest.raises(store.KitchenError):
        k.set_meal_plan([{**MEALS[0], "meal": "ceia"}])
    with pytest.raises(store.KitchenError):
        k.set_meal_plan([MEALS[0], MEALS[0]])
    bad = {**MEALS[0], "ingredients": [{"item": "sal", "qty": 1, "unit": "pitada"}]}
    with pytest.raises(store.KitchenError):
        k.set_meal_plan([bad])
    assert "2 refeição" in k.set_meal_plan(MEALS)["summary"]
    plan = k.meal_plan()
    assert plan["week_start"] == MONDAY and list(plan["days"]) == ["seg 16/11", "ter 17/11"]
    assert k.set_meal_plan(MEALS[:1])["ok"]  # replaces the week
    assert len(k.meal_plan()["days"]) == 1


def test_meals_without_ingredients_and_items_at_home(conn: sqlite3.Connection) -> None:
    k = kt()
    tilapia = {"item": "tilápia", "qty": 150, "unit": "g", "search": "tilapia", "have": True}
    meals = [
        {"day": 4, "meal": "jantar", "title": "Delivery", "ingredients": []},
        {"day": 5, "meal": "jantar", "title": "Sobras"},
        {**MEALS[0], "ingredients": [*MEALS[0]["ingredients"], tilapia]},
    ]
    assert k.set_meal_plan(meals)["ok"]
    items = {i["item"] for i in k.shopping_list()["items"]}
    assert "arroz" in items and "tilápia" not in items  # already at home
    week = k.page()["week"]["meals"]
    assert any(i.get("have") for m in week for i in m["ingredients"])


def test_shopping_list_with_prices(conn: sqlite3.Connection) -> None:
    src = FakeSource()
    k = kt(src)
    k.set_meal_plan(MEALS)
    no_location = k.shopping_list()
    assert "sem localização" in no_location["prices"] and src.calls == []
    k.set_kitchen_rules(weekly_budget=100, home_geohash="6gkzqf")
    out = k.shopping_list()
    items = {i["item"]: i for i in out["items"]}
    # 2 portions (no profiles yet): arroz 2 meals x 80 g x 2 = 320 g, ovos 8
    assert items["arroz"]["qty"] == "320 g" and items["ovo"]["qty"] == "8 un"
    assert set(src.calls) == {"arroz", "feijao", "ovos", "banana"}
    # banana (by weight) only exists in A: covering every item beats a cheaper single store.
    # Rice: 1 kg for R$ 7 in B beats A's 5 kg pack; eggs: a dozen in A beats B's 30.
    assert out["markets"] == ["MERCADO A", "MERCADO B"] and out["without_price"] == []
    assert items["arroz"]["where"] == "MERCADO B" and items["arroz"]["cost"] == "R$ 7,00"
    assert items["arroz"]["unit_price"] == "R$ 7,00/kg"
    assert items["ovo"]["where"] == "MERCADO A" and items["banana"]["cost"] == "R$ 1,20"
    assert out["estimated_total"] == "R$ 26,20"
    assert out["vs_budget"] == "R$ 26,20 de R$ 100,00 por semana"
    k.shopping_list()
    assert len(src.calls) == 4  # 24 h cache
    assert k.set_market("MERCADO A")["ok"]
    only_a = k.shopping_list()
    a_items = {i["item"]: i for i in only_a["items"]}
    assert only_a["markets"] == ["MERCADO A"] and a_items["banana"]["where"] == "MERCADO A"
    assert a_items["arroz"]["cost"] == "R$ 25,00"
    assert a_items["banana"]["cost"] == "R$ 1,20"  # 240 g at R$ 5,00/kg, by weight


@pytest.mark.parametrize(
    ("desc", "term", "ok"),
    [
        ("ARROZ BURITI POLIDO TIPO 1 PCT 5KG", "arroz", True),
        ("DOG CHOW ADULTO CARNE AMP ARROZ 15X85G", "arroz", False),
        ("OVO BRANCO GRANDE DZ", "ovos", True),
        ("MASSA JOIA PARAFUSO COM OVOS 500GR", "ovos", False),
        ("FEIJAO PRETO 1KG", "feijao preto", True),
    ],
)
def test_relevant(desc: str, term: str, ok: bool) -> None:
    assert prices.relevant(desc, term) is ok


def test_choose_takes_second_market_only_when_worth_it() -> None:
    needs = [store.Need("a", "a", "un", 1, 1), store.Need("b", "b", "un", 1, 1)]

    def off(m: str, c: int) -> prices.Offer:
        return prices.Offer(m, m, "X", c, 1, "un", "2026-11-15")

    small = [{"A": (10_00, off("A", 10_00)), "B": (9_00, off("B", 9_00))},
             {"A": (10_00, off("A", 10_00)), "B": (12_00, off("B", 12_00))}]  # fmt: skip
    plan = prices.choose(needs, small)
    assert plan is not None and len(plan.markets) == 1
    big = [{"A": (100_00, off("A", 100_00)), "B": (60_00, off("B", 60_00))},
           {"A": (50_00, off("A", 50_00)), "B": (90_00, off("B", 90_00))}]  # fmt: skip
    plan = prices.choose(needs, big)
    assert plan is not None and sorted(plan.markets) == ["A", "B"] and plan.total == 110_00
    assert prices.choose(needs, [{}, {}]) is None


def test_failed_source_does_not_break(conn: sqlite3.Connection) -> None:
    import httpx

    def broken(term: str, geohash: str, radius_km: int) -> list[dict[str, Any]]:
        raise httpx.ConnectError("down")

    k = KitchenTools(config.db_path(), lambda: TODAY, lambda: "ana", broken)
    k.set_meal_plan(MEALS)
    k.set_kitchen_rules(home_geohash="6gkzqf")
    out = k.shopping_list()
    assert out["prices_failed"] == 4 and "markets" not in out


def test_markets_from_osm_and_noted_prices(conn: sqlite3.Connection) -> None:
    from grana_mcp.kitchen import geo

    lat, lon = geo.decode("6gkzqf")
    assert geo.geohash(lat, lon) == "6gkzqf" and round(geo.distance_km((0, 0), (0, 1))) == 111
    k = kt()
    with pytest.raises(store.KitchenError, match="localização"):
        k.markets(discover=True)
    k.set_kitchen_rules(home_geohash="6gkzqf")
    found = k.markets(discover=True)
    by_name = {m["name"]: m for m in found["markets"]}
    assert set(by_name) == {"Zamprogna", "Sacolão da Vila"}  # food shops with a name only
    dists = [m["dist_km"] for m in found["markets"]]
    assert dists == sorted(dists)  # nearest first
    assert by_name["Zamprogna"]["kind"] == "supermercado"
    assert "Rua X 10" in by_name["Zamprogna"]["address"]
    assert "Anotado" in k.note_price("Arroz", 27.90, "zamprogna", 5, "kg", "Tio João")["summary"]
    k.note_price("banana", 4.99, "Sacolão", unit="kg")  # per kg, any amount
    k.note_price("ovos", 14.0, "Zamprogna", 1, "dz")
    k.note_price("feijao", 7.49, "Mercadinho do Zé", 1, "kg")  # new market, favorited
    with pytest.raises(store.KitchenError):
        k.note_price("arroz", -1, "Zamprogna")
    offers = k.find_prices("arroz")["offers"]
    assert offers[0].startswith("Zamprogna: Tio João · 5 kg R$ 27,90 (R$ 5,58/kg")
    k.set_meal_plan(MEALS)
    out = k.shopping_list()  # no price source: only noted prices
    assert "anotados" in out["prices"] and out["estimated_total"]
    items = {i["item"]: i for i in out["items"]}
    assert items["arroz"]["cost"] == "R$ 27,90"  # one 5 kg pack covers 320 g
    assert "Zamprogna" in out["markets"]
    names = [m["name"] for m in k.markets()["markets"]]
    assert set(names[:3]) == {"Zamprogna", "Sacolão da Vila", "Mercadinho do Zé"}  # favorites


def test_mcp_tools_and_page(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("GRANA_ACTOR", "bia")
    server = build_server(config.db_path(), lambda: TODAY, FakeSource())
    result = asyncio.run(server.call_tool("set_diet_profile", {"person": "Bia", "goal": "manter"}))
    assert isinstance(result, CallToolResult) and not result.is_error
    with pytest.raises(ToolError, match="não encontrada"):
        asyncio.run(server.call_tool("set_diet_profile", {"person": "Zé", "goal": "manter"}))
    with pytest.raises(store.KitchenError):
        kt().shopping_list("2026-11-16")  # no menu yet
    with pytest.raises(ToolError, match="sem cardápio"):
        asyncio.run(server.call_tool("shopping_list", {}))
    page = kt().page()
    assert page["shopping"] is None and page["status"]["people"][0]["person"] == "Bia"


def test_kitchen_alerts(conn: sqlite3.Connection) -> None:
    from datetime import datetime

    from grana_mcp.notify import alerts
    from grana_mcp.providers.pluggy import SAO_PAULO

    sunday = datetime(2026, 11, 22, 18, tzinfo=SAO_PAULO)
    assert alerts.kitchen_alerts(conn, sunday) == []  # no diet yet: no nagging
    k = kt()
    k.set_diet_profile("ana", goal="perder")
    k.set_kitchen_rules(delivery_max_week=1)
    keys = [a.key for a in alerts.kitchen_alerts(conn, sunday)]
    assert keys == ["plan:2026-11-23"]
    evening = datetime(2026, 11, 18, 20, 30, tzinfo=SAO_PAULO)
    assert [a.key for a in alerts.kitchen_alerts(conn, evening)] == ["day:2026-11-18"]
    k.log_day(True)
    assert alerts.kitchen_alerts(conn, evening) == []
    monday = datetime(2026, 11, 16, 9, tzinfo=SAO_PAULO)
    (weigh,) = alerts.kitchen_alerts(conn, monday)
    assert weigh.key == "weigh:2026-11-16" and "Ana" in weigh.body
    conn.executescript(
        "INSERT INTO profiles VALUES ('x', 'X', 'x');"
        "INSERT INTO items (id, profile_id) VALUES ('i', 'x');"
        "INSERT INTO accounts (id, item_id, kind, name) VALUES ('c', 'i', 'checking', 'C');"
    )
    for d in ("2026-11-16", "2026-11-17"):
        conn.execute(
            "INSERT INTO transactions (id, account_id, profile_id, posted_date, amount_cents, "
            "description_raw, category, status, source, synced_at) "
            "VALUES (?, 'c', 'x', ?, -4000, 'IFD*SINTETICO', 'Delivery', 'POSTED', 'pluggy', 'x')",
            (f"d{d}", d),
        )
    conn.commit()
    deliv = [a for a in alerts.kitchen_alerts(conn, evening) if a.key.startswith("deliv:")]
    assert deliv and deliv[0].title == "Delivery: 2 pedidos na semana"


def test_kitchen_web_api(conn: sqlite3.Connection, tmp_path: Any) -> None:
    from starlette.testclient import TestClient

    from grana_mcp.kitchen import geo
    from grana_mcp.web import auth
    from grana_mcp.web import claude_runner as cr
    from grana_mcp.web.app import create_app

    assert geo.geohash(-25.4284, -49.2733, 7) == "6gkzqfb"
    with pytest.raises(ValueError):
        geo.geohash(99, 0)
    app = create_app(
        cr.RunnerConfig(claude_bin="/bin/false", workdir=tmp_path),
        price_source=FakeSource(),
        shops=FakeShops(),
    )
    client = TestClient(app)
    assert client.get("/api/kitchen").status_code == 401
    h = {"Authorization": f"Bearer {auth.add_user('ana')}"}
    page = client.get("/api/kitchen", headers=h).json()
    assert page["status"]["people"] == [] and page["markets"] == []
    loc = client.post("/api/kitchen/location", json={"lat": -25.4284, "lon": -49.2733}, headers=h)
    assert loc.json()["status"]["rules"]["location_set"] is True
    rules = store.rules(conn)
    assert rules is not None and rules["home_geohash"] == "6gkzqf"  # coarse (~1 km)
    found = client.post("/api/kitchen/discover", json={}, headers=h).json()
    assert {m["name"] for m in found["markets"]} == {"Zamprogna", "Sacolão da Vila"}
    code = found["markets"][0]["code"]
    fav = client.post("/api/kitchen/market", json={"code": code, "favorite": True}, headers=h)
    assert any(m["favorite"] for m in fav.json()["markets"])
    client.post("/api/kitchen/day", json={"followed": True}, headers=h)
    assert client.get("/api/kitchen", headers=h).json()["answered_today"] is True
    bad = client.post("/api/kitchen/weight", json={"person": "Zé", "kg": 70}, headers=h)
    assert bad.status_code == 400 and "não encontrada" in bad.json()["error"]
    ok = client.post("/api/kitchen/weight", json={"person": "ana", "kg": 70.2}, headers=h)
    assert ok.status_code == 200
    assert client.post("/api/kitchen/nada", json={}, headers=h).status_code == 404
    assert client.post("/api/kitchen/location", json={"lat": "x"}, headers=h).status_code == 400


def test_menor_preco_retries_dropped_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    monkeypatch.setattr(prices, "MIN_INTERVAL_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("dropped", request=request)
        return httpx.Response(200, json={"categorias": [{"id": 2, "desc": "Leite", "qtd": 9}]})

    mp = prices.MenorPreco(httpx.Client(transport=httpx.MockTransport(handler)))
    assert mp.category("leite", "6gkzmg", 2) == 2 and len(calls) == 3
    calls.clear()

    def always(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("dropped", request=request)

    mp = prices.MenorPreco(httpx.Client(transport=httpx.MockTransport(always)))
    assert mp.category("leite", "6gkzmg", 2) is None and len(calls) == prices.CONNECT_TRIES


def test_prices_off(conn: sqlite3.Connection) -> None:
    k = KitchenTools(config.db_path(), lambda: TODAY, fetch=None)
    k.set_kitchen_rules(home_geohash="6gkzqf")
    k.set_meal_plan(MEALS)
    assert "anotados" in k.shopping_list()["prices"]
    with pytest.raises(store.KitchenError, match="indisponível"):
        k.markets(True)  # no shop source either
    assert k.find_prices("arroz")["offers"] == []
