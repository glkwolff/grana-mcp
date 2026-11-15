"""Command line: ``profile``, ``person``, ``account``, ``household``, ``sync``,
``reprocess``, ``status`` and ``serve``.

Never prints secrets. Output is PT-BR.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import keyring.errors
import typer

from grana_mcp import config
from grana_mcp.config import Credentials, CredentialsNotFoundError, Profile
from grana_mcp.money import format_brl
from grana_mcp.pipeline.categorize import (
    MATCH_TYPES,
    TAXONOMY,
    RuleError,
    add_user_rule,
    delete_user_rule,
    list_user_rules,
    run_categorize,
)
from grana_mcp.pipeline.dedupe import DedupeResult, run_dedupe
from grana_mcp.privacy.documents import parse_document
from grana_mcp.providers import ofx
from grana_mcp.providers.base import Item
from grana_mcp.providers.pluggy import PluggyAuthError, PluggyClient, PluggyError
from grana_mcp.store import db
from grana_mcp.sync import sync_profile, today_sp

CONSENT_WARN_DAYS = 30

app = typer.Typer(
    help="grana-mcp: finanças pessoais via Open Finance (Meu Pluggy), somente leitura.",
    no_args_is_help=True,
)
profile_app = typer.Typer(
    help="Perfis: credenciais da Pluggy e itemIds (um perfil pode ter contas de várias pessoas).",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")
person_app = typer.Typer(
    help="Pessoas da casa (donas das contas; um perfil pode ter várias).", no_args_is_help=True
)
app.add_typer(person_app, name="person")
account_app = typer.Typer(help="Contas sincronizadas e seus donos.", no_args_is_help=True)
app.add_typer(account_app, name="account")
household_app = typer.Typer(
    help="Documentos (CPF/CNPJ) da casa, guardados só como hash.", no_args_is_help=True
)
app.add_typer(household_app, name="household")
category_app = typer.Typer(
    help="Regras de categoria do usuário (têm prioridade sobre as embutidas).",
    no_args_is_help=True,
)
app.add_typer(category_app, name="category")


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


def _run_pipeline(conn: sqlite3.Connection) -> None:
    """Dedupe then categorize (no network); prints a short summary."""
    _echo_dedupe(run_dedupe(conn))
    _echo_categorize(conn)


def _echo_categorize(conn: sqlite3.Connection) -> None:
    r = run_categorize(conn)
    outros = r.by_category.get("Outros", 0)
    typer.echo(
        f"categorias: {r.total} transações, {outros} em 'Outros'; {r.changed} alteração(ões)."
    )


def _echo_dedupe(r: DedupeResult) -> None:
    by = r.by_reason
    typer.echo(
        f"limpeza: {by['card_bill_payment']} pagamento(s) de fatura, "
        f"{by['internal_transfer']} transferência(s) interna(s), {by['investment']} "
        f"investimento(s), {by['refund_pair']} estorno(s) pareado(s) fora do gasto; "
        f"{r.changed} alteração(ões)."
    )
    if r.flags["unmatched_bill_payment"]:
        typer.echo(
            f"aviso: {r.flags['unmatched_bill_payment']} pagamento(s) de fatura sem cartão "
            "sincronizado continuam contados como gasto."
        )
    if r.flags["unmatched_refund"]:
        typer.echo(f"aviso: {r.flags['unmatched_refund']} estorno(s) sem compra correspondente.")


@app.command()
def sync(
    profile: Annotated[str | None, typer.Option(help="Id do perfil (padrão: todos).")] = None,
    identity: Annotated[
        bool,
        typer.Option(
            "--identity/--no-identity",
            help="Lê o CPF/CNPJ do titular de cada item (GET /identity) e guarda só o hash, "
            "para reconhecer transferências internas.",
        ),
    ] = True,
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
                r = sync_profile(store, client, p, identity=identity)
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
        if r.identities_learned:
            typer.echo(
                f"[{p.id}] {r.identities_learned} documento(s) de titular aprendido(s) "
                "(guardado(s) só como hash)."
            )
        for w in r.warnings:
            typer.echo(f"[{p.id}] aviso: {w}")
    # Dedupe of the whole database, also when a profile failed.
    _run_pipeline(store.conn)
    store.conn.close()
    if failed:
        raise typer.Exit(1)


@contextmanager
def store_session() -> Iterator[db.Store]:
    """Open the store; roll back anything uncommitted and close on exit (also on errors)."""
    store = open_store()
    try:
        yield store
    finally:
        store.conn.rollback()
        store.conn.close()


@app.command()
def reprocess() -> None:
    """Recalcula a limpeza (fatura, transferência interna, investimento, estorno). Sem rede."""
    with store_session() as store:
        _run_pipeline(store.conn)


# --- people, accounts and household documents ---


def _person_or_exit(people: db.People, ref: str) -> sqlite3.Row:
    row = people.find(ref)
    if row is None:
        typer.echo(f"Pessoa '{ref}' não existe. Rode: grana-mcp person add <nome>", err=True)
        raise typer.Exit(1)
    return row


def _account_text(a: sqlite3.Row) -> str:
    owner = a["owner_name"] or f"(sem dono; perfil {a['profile_name'] or a['profile_id']})"
    return (
        f"{a['id']}  {a['name'] or '?'} {a['number_masked'] or ''} [{a['kind']}] "
        f"item {a['item_id']}: {owner}"
    )


@person_app.command("add")
def person_add(name: Annotated[str, typer.Argument(help="Nome de exibição.")]) -> None:
    """Cadastra uma pessoa da casa."""
    pid = config.slugify(name)
    with store_session() as store:
        try:
            db.People(store.conn, store.salt).add(pid, name.strip())
        except sqlite3.IntegrityError:
            typer.echo(f"A pessoa '{pid}' já existe.", err=True)
            raise typer.Exit(1) from None
        store.conn.commit()
    typer.echo(f"Pessoa '{pid}' criada. Atribua contas: grana-mcp account assign <conta> {pid}")


@person_app.command("list")
def person_list() -> None:
    """Lista as pessoas, quantas contas e documentos cada uma tem."""
    with store_session() as store:
        rows = db.People(store.conn, store.salt).all()
    if not rows:
        typer.echo("Nenhuma pessoa cadastrada: cada perfil conta como uma pessoa.")
    for r in rows:
        typer.echo(
            f"{r['id']}  {r['display_name']}: {r['n_accounts']} conta(s), "
            f"{r['n_docs']} documento(s)"
        )


@account_app.command("list")
def account_list() -> None:
    """Lista as contas sincronizadas e o dono de cada uma."""
    with store_session() as store:
        rows = db.People(store.conn, store.salt).accounts()
    if not rows:
        typer.echo("Nenhuma conta sincronizada. Rode: grana-mcp sync")
    for a in rows:
        typer.echo(_account_text(a))


@account_app.command("assign")
def account_assign(
    account: Annotated[str, typer.Argument(help="Id da conta ou final do número (ex.: 0001).")],
    person: Annotated[str, typer.Argument(help="Id ou nome da pessoa.")],
) -> None:
    """Define o dono de uma conta e recalcula a limpeza."""
    with store_session() as store:
        people = db.People(store.conn, store.salt)
        who = _person_or_exit(people, person)
        try:
            acc = people.find_account(account)
        except db.AmbiguousAccountError:
            typer.echo(f"'{account}' corresponde a mais de uma conta; use o id:", err=True)
            for a in people.accounts():
                typer.echo(f"  {_account_text(a)}", err=True)
            raise typer.Exit(1) from None
        if acc is None:
            typer.echo(f"Conta '{account}' não encontrada. Veja: grana-mcp account list", err=True)
            raise typer.Exit(1)
        people.assign(acc["id"], who["id"])
        store.conn.commit()
        typer.echo(f"Conta {acc['name'] or acc['id']} {acc['number_masked'] or ''} -> {who['id']}")
        _run_pipeline(store.conn)


@household_app.command("add-doc")
def household_add_doc(
    person: Annotated[str, typer.Argument(help="Id ou nome da pessoa (ou id do perfil).")],
) -> None:
    """Registra um CPF/CNPJ da casa. Só o hash com sal local é guardado."""
    with store_session() as store:
        people = db.People(store.conn, store.salt)
        who = people.find(person)
        profile = None if who is not None else config.get_profile(person)
        if who is None and profile is None:
            _person_or_exit(people, person)  # exits
        value = typer.prompt("CPF ou CNPJ (não aparece na tela)", hide_input=True)
        digits = parse_document(value)
        if digits is None:
            typer.echo("Documento inválido (confira os dígitos). Nada foi salvo.", err=True)
            raise typer.Exit(1)
        if profile is not None:
            store.upsert_profile(profile.id, profile.display_name)
        new = people.add_doc(
            digits,
            person_id=who["id"] if who is not None else None,
            profile_id=profile.id if profile is not None else None,
        )
        store.conn.commit()
        kind = "CPF" if len(digits) == 11 else "CNPJ"
        owner = who["id"] if who is not None else profile.id if profile is not None else "?"
        typer.echo(
            f"{kind} {'registrado' if new else 'já existia; dono atualizado'} para '{owner}' "
            "(só o hash foi guardado)."
        )
        _run_pipeline(store.conn)


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

    people = db.People(store.conn, store.salt)
    owners = {a["id"]: a for a in people.accounts()}
    accounts = store.account_counts()
    if accounts:
        typer.echo("\nContas (transações guardadas; dono):")
        for a in accounts:
            o = owners.get(a["id"])
            owner = (o["owner_name"] if o else None) or "sem dono (conta como o perfil)"
            typer.echo(
                f"  {a['name']} {a['number_masked'] or ''} [{a['kind']}] item {a['item_id']}: "
                f"{a['n']}; {owner}"
            )
    plist = people.all()
    if plist:
        typer.echo("\nPessoas: " + ", ".join(f"{r['display_name']} ({r['id']})" for r in plist))

    rows = store.monthly_spending(_first_month(today_sp(), 12))
    if rows:
        typer.echo(
            "\nSaídas por mês e conta (sem pagamento de fatura, transferências internas, "
            "investimentos e estornos pareados; veja 'grana-mcp reprocess'):"
        )
        for r in rows:
            typer.echo(
                f"  {r['month']}  {r['name']} [{r['kind']}]: {format_brl(-r['spent_cents'])} "
                f"({r['n']} transações)"
            )
    store.conn.close()


# --- category rules ---


@category_app.command("add")
def category_add(
    pattern: str,
    category: str,
    match_type: str = typer.Option("contains", help="contains | regex | merchant"),
    priority: int = typer.Option(0, help="Maior vence entre regras do usuário."),
) -> None:
    """Cria uma regra (ex.: grana-mcp category add "padaria do bairro" Mercado) e recategoriza."""
    if match_type not in MATCH_TYPES:
        typer.echo("match_type deve ser contains, regex ou merchant.", err=True)
        raise typer.Exit(1)
    with store_session() as store:
        try:
            rule = add_user_rule(store.conn, pattern, category, match_type, priority)
        except RuleError as exc:
            typer.echo(f"Regra inválida: {exc}", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Regra {rule.rule_id}: '{rule.pattern}' ({rule.match_type}) -> {rule.category}")
        if rule.category not in TAXONOMY:
            typer.echo("aviso: categoria fora da lista padrão: " + ", ".join(TAXONOMY))
        _echo_categorize(store.conn)


@category_app.command("list")
def category_list() -> None:
    """Lista as regras do usuário."""
    with store_session() as store:
        rules = list_user_rules(store.conn)
        if not rules:
            typer.echo("Nenhuma regra do usuário.")
        for r in rules:
            typer.echo(
                f"{r.rule_id}: '{r.pattern}' ({r.match_type}, prioridade {r.priority})"
                f" -> {r.category}"
            )


@category_app.command("remove")
def category_remove(rule_id: int) -> None:
    """Remove uma regra do usuário pelo id e recategoriza."""
    with store_session() as store:
        if not delete_user_rule(store.conn, rule_id):
            typer.echo(f"Regra {rule_id} não existe.", err=True)
            raise typer.Exit(1)
        typer.echo(f"Regra {rule_id} removida.")
        _echo_categorize(store.conn)


# --- file import (OFX / CSV) ---


def _import_statement(stmt: ofx.Statement, profile: str, person: str | None) -> None:
    with store_session() as store:
        cfg = config.get_profile(profile)
        name = cfg.display_name if cfg else profile
        store.upsert_profile(profile, name)
        store.upsert_item(
            profile,
            Item(
                id=stmt.account.item_id,
                connector_name="Arquivo (OFX/CSV)",
                status="FILE",
                consent_expires_at=None,
                last_updated_at=None,
            ),
        )
        store.upsert_account(stmt.account)
        counts = store.upsert_transactions(profile, stmt.transactions, source="ofx")
        store.mark_item_synced(stmt.account.item_id)
        if person:
            people = db.People(store.conn, store.salt)
            row = _person_or_exit(people, person)
            people.assign(stmt.account.id, row["id"])
        store.conn.commit()
        typer.echo(
            f"{stmt.account.name}: {len(stmt.transactions)} transações lidas; "
            f"{counts.new} novas, {counts.updated} atualizadas."
        )
        _run_pipeline(store.conn)


@app.command("import-ofx")
def import_ofx(
    path: Path,
    profile: Annotated[str, typer.Option(help="Perfil dono do arquivo (ex.: ana).")],
    label: Annotated[str | None, typer.Option(help="Nome da conta (opcional).")] = None,
    person: Annotated[str | None, typer.Option(help="Pessoa dona da conta (opcional).")] = None,
) -> None:
    """Importa um extrato OFX (conta ou cartão). Sem rede; reimportar não duplica.

    Não importe uma conta que também é sincronizada pela Pluggy: contaria tudo duas vezes.
    """
    try:
        stmt = ofx.parse_ofx(ofx.read_text(path), config.slugify(profile), label)
    except (ofx.ImportFormatError, OSError) as exc:
        typer.echo(f"Não consegui ler o OFX: {exc}", err=True)
        raise typer.Exit(1) from None
    _import_statement(stmt, config.slugify(profile), person)


@app.command("import-csv")
def import_csv(
    path: Path,
    profile: Annotated[str, typer.Option(help="Perfil dono do arquivo (ex.: ana).")],
    fmt: Annotated[
        str, typer.Option("--format", help="nubank-cartao | nubank-conta | generic")
    ] = "generic",
    label: Annotated[str | None, typer.Option(help="Nome da conta (opcional).")] = None,
    person: Annotated[str | None, typer.Option(help="Pessoa dona da conta (opcional).")] = None,
    invert: Annotated[bool, typer.Option(help="Inverte o sinal dos valores.")] = False,
) -> None:
    """Importa um CSV exportado do banco. Sem rede; reimportar não duplica."""
    if fmt not in ofx.CSV_FORMATS:
        typer.echo(f"--format deve ser um de: {', '.join(ofx.CSV_FORMATS)}", err=True)
        raise typer.Exit(1)
    try:
        stmt = ofx.parse_csv(
            ofx.read_text(path),
            config.slugify(profile),
            fmt,
            label,
            invert=invert,
        )
    except (ofx.ImportFormatError, OSError) as exc:
        typer.echo(f"Não consegui ler o CSV: {exc}", err=True)
        raise typer.Exit(1) from None
    _import_statement(stmt, config.slugify(profile), person)


# --- doctor ---

SECRET_ENV_VARS = (config.ENV_CLIENT_SECRET,)


def _check(ok: bool | None, msg: str) -> bool:
    mark = {True: "ok ", False: "ERRO", None: "aviso"}[ok]
    typer.echo(f"[{mark}] {msg}")
    return ok is not False


@app.command()
def doctor(
    offline: Annotated[bool, typer.Option(help="Não testa a autenticação na Pluggy.")] = False,
) -> None:
    """Checa credenciais, autorização, permissões do banco e versão do schema."""
    healthy = True
    profiles = config.load_profiles()
    healthy &= _check(bool(profiles) or None, f"{len(profiles)} perfil(is) configurado(s)")
    for var in SECRET_ENV_VARS:
        if os.environ.get(var):
            _check(None, f"{var} está no ambiente: prefira o keyring ('grana-mcp profile add')")
    for p in profiles:
        try:
            creds = config.load_credentials(p.id)
        except CredentialsNotFoundError:
            healthy &= _check(False, f"perfil {p.id}: credenciais não encontradas")
            continue
        _check(True, f"perfil {p.id}: credenciais encontradas")
        if offline:
            continue
        try:
            with make_client(creds) as client:
                client.authenticate()
                for item_id in p.item_ids:
                    item = client.get_item(item_id)
                    exp = item.consent_expires_at
                    if exp is None:
                        _check(None, f"item {item_id}: sem data de expiração da autorização")
                        continue
                    days = (exp - datetime.now(UTC)).days
                    ok = None if days <= 30 else True
                    healthy &= _check(
                        False if days < 0 else ok,
                        f"item {item_id}: {item.status or '?'}; autorização expira em "
                        f"{exp.date().isoformat()} ({days} dias)",
                    )
        except PluggyAuthError:
            healthy &= _check(False, f"perfil {p.id}: a Pluggy recusou o Client ID/Secret")
        except PluggyError as exc:
            healthy &= _check(False, f"perfil {p.id}: erro na Pluggy ({exc.status})")
    path = config.db_path()
    if not path.exists():
        healthy &= _check(False, "banco de dados não existe; rode 'grana-mcp sync'")
    else:
        mode = stat.S_IMODE(path.stat().st_mode)
        healthy &= _check(mode & 0o077 == 0 or False, f"permissões do banco: {oct(mode)}")
        dmode = stat.S_IMODE(path.parent.stat().st_mode)
        healthy &= _check(dmode & 0o077 == 0 or False, f"permissões da pasta: {oct(dmode)}")
        conn = db.connect(path)
        try:
            version = db.schema_version(conn)
            last = conn.execute(
                "SELECT MAX(finished_at) FROM sync_runs WHERE status='ok'"
            ).fetchone()
        finally:
            conn.close()
        healthy &= _check(version == db.LATEST_VERSION, f"schema v{version}")
        _check(
            bool(last and last[0]) or None,
            f"último sync ok: {last[0] if last and last[0] else 'nunca'}",
        )
    if not healthy:
        raise typer.Exit(1)


# --- init ---

MEU_PLUGGY_URL = "https://meu.pluggy.ai"
DASHBOARD_URL = "https://dashboard.pluggy.ai"


@app.command()
def init(
    open_browser: Annotated[bool, typer.Option(help="Abre as páginas no navegador.")] = True,
) -> None:
    """Assistente: Meu Pluggy -> credenciais -> primeiro sync -> comando do cliente MCP."""
    typer.echo(
        "grana-mcp: suas finanças no seu computador, via Open Finance (Meu Pluggy, gratuito "
        "para uso pessoal).\n\n"
        f"1) Em {MEU_PLUGGY_URL}, crie a conta e conecte seus bancos (cada banco é autorizado "
        "no app dele; escolha 12 meses).\n"
        f"2) Em {DASHBOARD_URL}, entre com o MESMO e-mail. Em 'Aplicações', crie uma aplicação "
        "(desenvolvimento serve; não precisa ir para produção) e guarde o Client ID e o Client "
        "Secret (o secret só aparece uma vez).\n"
        "3) Clique no ▶ (Demo) dessa aplicação, escolha o conector 'MeuPluggy', faça login e "
        "autorize. Anote o itemId de cada banco (formato UUID).\n"
        "   Os itemIds precisam ser da MESMA aplicação do Client ID.\n"
    )
    if open_browser and typer.confirm("Abrir essas páginas no navegador?", default=True):
        for url in (MEU_PLUGGY_URL, DASHBOARD_URL):
            webbrowser.open(url)
    typer.confirm("Terminou os passos 1 a 3?", default=True, abort=True)
    profile_add(display_name=None, profile_id=None, item_ids=None)
    typer.echo("\nPrimeiro sync (pode levar alguns minutos)...")
    sync(profile=None, identity=True)
    typer.echo(
        "\nPronto. Para usar no Claude Code:\n"
        "  claude mcp add grana -- uvx grana-mcp serve\n"
        "Depois, pergunte por exemplo: 'quanto gastamos por categoria no mês passado?'.\n"
        "Para manter os dados atualizados, agende 'grana-mcp sync' 1x por dia (cron/systemd)."
    )


@app.command()
def serve() -> None:
    """Servidor MCP por stdio (é o que o cliente MCP executa). Só lê o SQLite local."""
    from grana_mcp.server import run  # imported lazily: the MCP SDK is heavy

    run()
