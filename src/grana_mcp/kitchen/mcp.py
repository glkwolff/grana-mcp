"""MCP registration of the kitchen tools (diet plan, weigh-ins, groceries, prices)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from grana_mcp.kitchen.service import DISCLAIMER, KitchenTools
from grana_mcp.kitchen.store import KitchenError

_RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_NET = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
_RW = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
NOTE = " Só altere quando a pessoa pedir ou confirmar."


def _safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except KitchenError as exc:
        raise ToolError(str(exc)) from None
    except (ValueError, TypeError) as exc:
        raise ToolError(f"valor inválido: {exc}") from None
    except httpx.HTTPError:
        raise ToolError("fonte de preços indisponível agora; tente mais tarde") from None


def register(server: MCPServer, k: KitchenTools) -> None:
    @server.tool(
        annotations=_RO,
        description="Cozinha da casa numa chamada: perfis da dieta com meta calórica, proteína, "
        "porção de cada um, peso e tendência; regras (teto semanal, delivery); cardápio, gasto "
        "com comida e delivery da semana; adesão; e 'todo' com o que falta combinar. Use antes "
        "de falar de dieta, cardápio ou compras. " + DISCLAIMER,
    )
    def kitchen_status() -> dict[str, Any]:
        return _safe(k.kitchen_status)

    @server.tool(
        annotations=_RW,
        description="Cria/atualiza o perfil de dieta de uma pessoa da casa (person: nome). Campos "
        "opcionais: sex ('f'/'m', só para a fórmula), birth_year, height_cm, activity "
        "(sedentario, leve, moderado, intenso), goal (perder, manter, ganhar), target_kg, "
        "deadline (AAAA-MM-DD), restrictions, likes, dislikes. Defina junto com a pessoa, "
        "perguntando o que faltar." + NOTE,
    )
    def set_diet_profile(
        person: str,
        sex: str | None = None,
        birth_year: int | None = None,
        height_cm: int | None = None,
        activity: str | None = None,
        goal: str | None = None,
        target_kg: float | None = None,
        deadline: str | None = None,
        restrictions: str | None = None,
        likes: str | None = None,
        dislikes: str | None = None,
    ) -> dict[str, Any]:
        return _safe(
            lambda: k.set_diet_profile(
                person,
                sex=sex,
                birth_year=birth_year,
                height_cm=height_cm,
                activity=activity,
                goal=goal,
                target_kg=target_kg,
                deadline=deadline,
                restrictions=restrictions,
                likes=likes,
                dislikes=dislikes,
            )
        )

    @server.tool(
        annotations=_RW,
        description="Regras da cozinha da casa: weekly_budget (R$ por semana com comida), "
        "delivery_max_week (pedidos por semana), notes (combinados) e radius_km (1-20, raio "
        "para buscar mercados)." + NOTE,
    )
    def set_kitchen_rules(
        weekly_budget: float | None = None,
        delivery_max_week: int | None = None,
        notes: str | None = None,
        radius_km: int | None = None,
    ) -> dict[str, Any]:
        return _safe(
            lambda: k.set_kitchen_rules(
                weekly_budget=weekly_budget,
                delivery_max_week=delivery_max_week,
                notes=notes,
                radius_km=radius_km,
            )
        )

    @server.tool(
        annotations=_RW,
        description="Registra o peso (kg) de uma pessoa num dia (day AAAA-MM-DD, padrão hoje).",
    )
    def log_weight(person: str, kg: float, day: str | None = None) -> dict[str, Any]:
        return _safe(lambda: k.log_weight(person, kg, day))

    @server.tool(
        annotations=_RW,
        description="Registra se a casa seguiu o plano num dia (followed true/false, note "
        "opcional, day AAAA-MM-DD, padrão hoje).",
    )
    def log_day(followed: bool, note: str | None = None, day: str | None = None) -> dict[str, Any]:
        return _safe(lambda: k.log_day(followed, note, day))

    @server.tool(
        annotations=_RW,
        description="Salva o cardápio COMPARTILHADO de uma semana (substitui o anterior). "
        "week_start: segunda AAAA-MM-DD (padrão: esta semana). meals: lista de {day 0=seg..6=dom, "
        "meal cafe|almoco|lanche|jantar, title, how (preparo curto), kcal e protein_g de UMA "
        "porção de referência (prato para quem come 2000 kcal/dia), ingredients: [{item, qty, "
        "unit g|kg|ml|l|un, search (nome genérico do produto, 1-2 palavras, ex. 'arroz', "
        "'feijao preto', 'banana'), have: true se já tem em casa (fica fora da lista de "
        "compras)}] por porção de referência; ingredients pode ser [] para delivery, sobras ou "
        "comer fora}. Cada pessoa come a própria porção (kitchen_status). Respeite restrições, "
        "gostos, metas e o teto semanal; prefira repetir ingredientes para comprar menos coisas."
        + NOTE,
    )
    def set_meal_plan(meals: list[dict[str, Any]], week_start: str | None = None) -> dict[str, Any]:
        return _safe(lambda: k.set_meal_plan(meals, week_start))

    @server.tool(
        annotations=_RO,
        description="Cardápio de uma semana (week_start segunda AAAA-MM-DD, padrão esta).",
    )
    def meal_plan(week_start: str | None = None) -> dict[str, Any]:
        return _safe(lambda: k.meal_plan(week_start))

    @server.tool(
        annotations=_NET,
        description="Lista de compras da semana (soma o cardápio para todos, menos o que já tem "
        "em casa) com onde comprar pelos preços anotados (note_price): escolhe 1 mercado, ou 2 "
        "se o segundo economizar bem. Mostra custo estimado vs. teto.",
    )
    def shopping_list(week_start: str | None = None) -> dict[str, Any]:
        return _safe(lambda: k.shopping_list(week_start))

    @server.tool(
        annotations=_NET,
        description="Preços anotados de um produto (mais barato por kg/l/unidade primeiro).",
    )
    def find_prices(term: str) -> dict[str, Any]:
        return _safe(lambda: k.find_prices(term))

    @server.tool(
        annotations=_NET,
        description="Lojas de comida perto de casa: supermercado, atacado, hortifrúti, açougue, "
        "padaria, feira (discover=true busca no OpenStreetMap; precisa da localização). Os "
        "favoritos são onde a lista de compras procura preço.",
    )
    def markets(discover: bool = False) -> dict[str, Any]:
        return _safe(lambda: k.markets(discover))

    @server.tool(
        annotations=_RW,
        description="Marca (favorite=true) ou desmarca um mercado como favorito, por nome ou "
        "código." + NOTE,
    )
    def set_market(market: str, favorite: bool = True) -> dict[str, Any]:
        return _safe(lambda: k.set_market(market, favorite))

    @server.tool(
        annotations=_RW,
        description="Anota um preço que alguém da casa viu ou pagou. item: produto genérico "
        "('arroz', 'peito frango'); price em reais; market: nome do mercado (vira favorito; se "
        "não existir, é criado); qty+unit (g, kg, ml, l, un, dz) = tamanho do pacote, ex. 5 kg; "
        "sem qty e com unit kg/l = preço por kg/l (granel); product: marca/descrição opcional; "
        "day AAAA-MM-DD (padrão hoje). Use sempre que alguém citar um preço." + NOTE,
    )
    def note_price(
        item: str,
        price: float,
        market: str,
        qty: float | None = None,
        unit: str | None = None,
        product: str | None = None,
        day: str | None = None,
    ) -> dict[str, Any]:
        return _safe(lambda: k.note_price(item, price, market, qty, unit, product, day))
