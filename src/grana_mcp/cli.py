"""Command line: ``grana-mcp profile add``, ``sync``, ``status`` and ``serve``.

Never prints secrets. Output is PT-BR.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime
from typing import Annotated

import keyring.errors
import typer

from grana_mcp import config
from grana_mcp.config import Credentials, CredentialsNotFoundError, Profile
from grana_mcp.money import format_brl
from grana_mcp.providers.pluggy import PluggyAuthError, PluggyClient, PluggyError
from grana_mcp.store import db
from grana_mcp.sync import sync_profile, today_sp

CONSENT_WARN_DAYS = 30

app = typer.Typer(
    help="grana-mcp: finanças pessoais via Open Finance (Meu Pluggy), somente leitura.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Gerencia perfis (uma pessoa = um perfil).", no_args_is_help=True)
app.add_typer(profile_app, name="profile")


def make_client(creds: Credentials) -> PluggyClient:
    """Factory (tests replace it with a client on a mock transport)."""
    return PluggyClient(creds.client_id, creds.client_secret)


def open_store() -> db.Store:
    conn = db.connect(config.db_path())
    return db.Store(conn, db.load_or_create_salt(config.data_dir()))


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log de progresso.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@profile_app.command("add")
def profile_add(
    display_name: Annotated[str | None, typer.Option(help="Nome de exibição.")] = None,
    profile_id: Annotated[str | None, typer.Option("--id", help="Id do perfil.")] = None,
    item_ids: Annotated[
        list[str] | None, typer.Option("--item-id", help="itemId da Pluggy (repita a opção).")
    ] = None,
) -> None:
    """Adiciona (ou atualiza) um perfil: credenciais da Pluggy e itemIds."""
    name = display_name or typer.prompt("Nome de exibição (ex.: Ana)")
    pid = profile_id or config.slugify(name)
    client_id = typer.prompt("Client ID da Pluggy").strip()
    client_secret = typer.prompt("Client Secret da Pluggy (não aparece na tela)", hide_input=True)
    ids = item_ids or [
        i.strip() for i in typer.prompt("itemIds (separados por vírgula)").split(",") if i.strip()
    ]
    creds = Credentials(client_id=client_id, client_secret=client_secret.strip())

    typer.echo("Testando as credenciais (POST /auth)...")
    try:
        with make_client(creds) as client:
            client.authenticate()
            for item_id in ids:
                item = client.get_item(item_id)
                typer.echo(f"  item {item.id}: {item.connector_name or '?'} ({item.status or '?'})")
    except PluggyAuthError:
        typer.echo("A Pluggy recusou o Client ID/Secret. Nada foi salvo.", err=True)
        raise typer.Exit(1) from None
    except PluggyError as exc:
        typer.echo(f"Erro na Pluggy: {exc}. Nada foi salvo.", err=True)
        raise typer.Exit(1) from None

    try:
        config.store_credentials(pid, creds)
        typer.echo("Credenciais salvas no keyring do sistema.")
    except keyring.errors.KeyringError:
        typer.echo(
            "Keyring do sistema indisponível; as credenciais NÃO foram salvas. Defina "
            f"{config.ENV_CLIENT_ID} e {config.ENV_CLIENT_SECRET} no ambiente do sync.",
            err=True,
        )
    config.upsert_profile(Profile(id=pid, display_name=name, item_ids=ids))
    typer.echo(f"Perfil '{pid}' salvo com {len(ids)} item(ns). Rode: grana-mcp sync")


@app.command()
def sync(
    profile: Annotated[str | None, typer.Option(help="Id do perfil (padrão: todos).")] = None,
) -> None:
    """Baixa até 365 dias de transações para o SQLite local."""
    profiles = config.load_profiles()
    if profile:
        profiles = [p for p in profiles if p.id == profile]
        if not profiles:
            typer.echo(f"Perfil '{profile}' não existe.", err=True)
            raise typer.Exit(1)
    if not profiles:
        typer.echo("Nenhum perfil configurado. Rode: grana-mcp profile add", err=True)
        raise typer.Exit(1)

    store = open_store()
    failed = False
    for p in profiles:
        try:
            creds = config.load_credentials(p.id)
        except CredentialsNotFoundError as exc:
            typer.echo(f"[{p.id}] {exc}", err=True)
            failed = True
            continue
        typer.echo(f"[{p.id}] sincronizando {len(p.item_ids)} item(ns)...")
        try:
            with make_client(creds) as client:
                r = sync_profile(store, client, p)
        except PluggyError as exc:
            typer.echo(f"[{p.id}] erro: {exc}", err=True)
            failed = True
            continue
        typer.echo(
            f"[{p.id}] ok: {r.items} item(ns), {r.accounts} conta(s), {r.fetched} transações "
            f"lidas; {r.new} novas, {r.updated} atualizadas, {r.deleted} removidas; "
            f"faturas fechadas: {r.bills_new} novas, {r.bills_updated} atualizadas."
        )
        if r.sign_mismatches:
            typer.echo(
                f"[{p.id}] aviso: {r.sign_mismatches} transações com 'type' e sinal de "
                "'amount' em desacordo (ver docs/pluggy-mapping.md, U4)."
            )
        for w in r.warnings:
            typer.echo(f"[{p.id}] aviso: {w}")
    store.conn.close()
    if failed:
        raise typer.Exit(1)


def consent_line(expires_iso: str | None, now: datetime) -> tuple[str, bool]:
    """Text about the consent expiry and whether it deserves a warning (<= 30 days)."""
    if not expires_iso:
        return "autorização: sem data de expiração", False
    expires = datetime.fromisoformat(expires_iso)
    days = (expires - now).days
    when = expires.astimezone(UTC).date().isoformat()
    if expires <= now:
        return f"autorização EXPIROU em {when}: renove no Pluggy Dashboard", True
    text = f"autorização expira em {when} ({days} dias)"
    if days <= CONSENT_WARN_DAYS:
        return f"ATENÇÃO: {text}. Renove no Pluggy Dashboard (Demo -> MeuPluggy)", True
    return text, False


def _first_month(today: date, months: int) -> date:
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


@app.command()
def status() -> None:
    """Perfis, itens, última sincronização, expiração da autorização e totais por mês."""
    profiles = config.load_profiles()
    if not profiles:
        typer.echo("Nenhum perfil configurado. Rode: grana-mcp profile add")
        return
    store = open_store()
    now = datetime.now(UTC)
    items_by_profile: dict[str, list[sqlite3.Row]] = {}
    for row in store.items():
        items_by_profile.setdefault(row["profile_id"], []).append(row)

    for p in profiles:
        typer.echo(f"Perfil {p.id} ({p.display_name})")
        run = store.last_run(p.id)
        if run is None:
            typer.echo("  nunca sincronizado")
        else:
            typer.echo(
                f"  último sync: {run['started_at']} [{run['status']}] "
                f"{run['n_new']} novas, {run['n_updated']} atualizadas, "
                f"{run['n_deleted']} removidas" + (f" erro: {run['error']}" if run["error"] else "")
            )
        known = {r["id"]: r for r in items_by_profile.get(p.id, [])}
        for item_id in p.item_ids:
            item_row = known.get(item_id)
            if item_row is None:
                typer.echo(f"  item {item_id}: ainda não sincronizado")
                continue
            text, _warn = consent_line(item_row["consent_expires_at"], now)
            typer.echo(
                f"  item {item_id}: {item_row['connector_name'] or '?'} "
                f"[{item_row['status'] or '?'}] "
                f"último sync {item_row['last_synced_at'] or '-'}; {text}"
            )

    accounts = store.account_counts()
    if accounts:
        typer.echo("\nContas (transações guardadas):")
        for a in accounts:
            typer.echo(
                f"  {a['name']} {a['number_masked'] or ''} [{a['kind']}] item {a['item_id']}: "
                f"{a['n']}"
            )

    rows = store.monthly_spending(_first_month(today_sp(), 12))
    if rows:
        typer.echo(
            "\nSaídas por mês e conta (soma dos valores negativos, SEM remover pagamento de "
            "fatura nem transferências internas — isso é a Fase 3):"
        )
        for r in rows:
            typer.echo(
                f"  {r['month']}  {r['name']} [{r['kind']}]: {format_brl(-r['spent_cents'])} "
                f"({r['n']} transações)"
            )
    store.conn.close()


@app.command()
def serve() -> None:
    """Servidor MCP por stdio (é o que o cliente MCP executa). Só lê o SQLite local."""
    from grana_mcp.server import run  # imported lazily: the MCP SDK is heavy

    run()
