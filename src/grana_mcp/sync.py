"""Sync one profile: provider (read-only) -> local SQLite.

Never logs amounts, descriptions or documents; only counts and ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from grana_mcp.config import Profile
from grana_mcp.pipeline.dedupe import DedupeResult, run_dedupe
from grana_mcp.providers.base import Account, Provider
from grana_mcp.providers.pluggy import SAO_PAULO, PluggyError
from grana_mcp.store.db import People, Store

log = logging.getLogger(__name__)

# Open Finance keeps at most 365 days of history (HANDOFF §2).
SYNC_DAYS = 365


@dataclass
class SyncResult:
    items: int = 0
    accounts: int = 0
    fetched: int = 0
    new: int = 0
    updated: int = 0
    deleted: int = 0
    sign_mismatches: int = 0
    bills_new: int = 0
    bills_updated: int = 0
    identities_learned: int = 0
    dedupe: DedupeResult | None = None
    warnings: list[str] = field(default_factory=list)


def today_sp() -> date:
    return datetime.now(SAO_PAULO).date()


def _sync_bills(store: Store, provider: Provider, account: Account, result: SyncResult) -> None:
    """Closed bills are optional: a connector without the product must not fail the sync."""
    try:
        bills = provider.list_bills(account)
    except PluggyError as exc:
        if exc.status in (401, 403, 429) or exc.status is None or exc.status >= 500:
            raise
        result.warnings.append(f"account {account.id}: bills unavailable ({exc.status})")
        return
    counts = store.upsert_bills(bills)
    result.bills_new += counts.new
    result.bills_updated += counts.updated


def _learn_identity(
    store: Store, provider: Provider, profile: Profile, item_id: str, result: SyncResult
) -> None:
    """Hash of the item holder's CPF/CNPJ -> household docs (once per item).

    Best effort: any provider error other than an auth failure only warns.
    """
    people = People(store.conn, store.salt)
    if people.has_item_identity(item_id):
        return
    try:
        digits = provider.get_identity_document(item_id)
    except PluggyError as exc:
        if exc.status == 401:
            raise
        result.warnings.append(f"item {item_id}: identity unavailable ({exc.status})")
        return
    if digits is None:
        return
    people.set_item_identity(item_id, digits, profile.id)
    result.identities_learned += 1


# A full sync at least this often, even if no item says it changed.
FULL_SYNC_EVERY = timedelta(hours=24)


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def changed_items(
    store: Store, provider: Provider, profile: Profile, now: datetime | None = None
) -> list[str]:
    """Items the provider refreshed after our last sync of them (one light GET per item:
    Meu Pluggy refreshes each item once a day, at a time of its own). Every item when the
    profile's last good sync is older than FULL_SYNC_EVERY."""
    now = now or datetime.now(UTC)
    row = store.conn.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE profile_id = ? AND status = 'ok'",
        (profile.id,),
    ).fetchone()
    last_ok = _when(row[0] if row else None)
    if last_ok is None or now - last_ok >= FULL_SYNC_EVERY:
        return list(profile.item_ids)
    out = []
    for item_id in profile.item_ids:
        seen = store.conn.execute(
            "SELECT last_synced_at FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        synced = _when(seen[0] if seen else None)
        item = provider.get_item(item_id)
        if synced is None or (item.last_updated_at is not None and item.last_updated_at > synced):
            out.append(item_id)
    return out


def sync_profile(
    store: Store,
    provider: Provider,
    profile: Profile,
    *,
    today: date | None = None,
    identity: bool = True,
) -> SyncResult:
    today = today or today_sp()
    date_from = today - timedelta(days=SYNC_DAYS)
    # Rows dated after this day are certainly inside the fetched window, whatever
    # the timezone conversion did at the edge.
    reconcile_from = date_from + timedelta(days=1)
    result = SyncResult()
    store.upsert_profile(profile.id, profile.display_name)
    run_id = store.start_run(profile.id)  # commits the profile row too
    try:
        for item_id in profile.item_ids:
            item = provider.get_item(item_id)
            store.upsert_item(profile.id, item)
            result.items += 1
            consent_lapsed = (
                item.consent_expires_at is not None and item.consent_expires_at <= datetime.now(UTC)
            )
            if consent_lapsed:
                result.warnings.append(
                    f"item {item_id}: authorization expired; reads come back empty. "
                    "Renew it in the Pluggy Dashboard (Demo -> Connect Account -> MeuPluggy)."
                )
            for account in provider.list_accounts(item_id):
                store.upsert_account(account)
                result.accounts += 1
                txs = list(provider.list_transactions(account, date_from))
                result.fetched += len(txs)
                result.sign_mismatches += sum(t.sign_mismatch for t in txs)
                counts = store.upsert_transactions(profile.id, txs)
                result.new += counts.new
                result.updated += counts.updated
                if txs and not consent_lapsed:
                    result.deleted += store.delete_missing(
                        account.id, reconcile_from, {t.id for t in txs}
                    )
                elif not txs and store.count_transactions(account.id, reconcile_from):
                    result.warnings.append(
                        f"account {account.id}: Pluggy returned no transactions; "
                        "kept the local ones"
                    )
                if account.kind == "credit_card":
                    _sync_bills(store, provider, account, result)
            if identity:
                _learn_identity(store, provider, profile, item_id, result)
            store.mark_item_synced(item_id)
            store.conn.commit()
            log.info("item %s synced", item_id)
    except Exception as exc:
        store.conn.rollback()
        # Only our own error messages are safe to persist; others (e.g. pydantic
        # validation errors) may echo transaction data.
        detail = str(exc) if isinstance(exc, PluggyError) else type(exc).__name__
        store.finish_run(
            run_id,
            status="error",
            n_new=result.new,
            n_updated=result.updated,
            n_deleted=result.deleted,
            error=detail,
        )
        raise
    store.finish_run(
        run_id, status="ok", n_new=result.new, n_updated=result.updated, n_deleted=result.deleted
    )
    # Pairs cross accounts and profiles, so the whole database is recomputed.
    result.dedupe = run_dedupe(store.conn)
    if result.sign_mismatches:
        log.warning(
            "%d transactions where Pluggy type and amount sign disagree", result.sign_mismatches
        )
    return result
