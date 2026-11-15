"""Sync one profile: provider (read-only) -> local SQLite.

Never logs amounts, descriptions or documents; only counts and ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from grana_mcp.config import Profile
from grana_mcp.providers.base import Provider
from grana_mcp.providers.pluggy import SAO_PAULO, PluggyError
from grana_mcp.store.db import Store

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
    warnings: list[str] = field(default_factory=list)


def today_sp() -> date:
    return datetime.now(SAO_PAULO).date()


def sync_profile(
    store: Store, provider: Provider, profile: Profile, *, today: date | None = None
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
    if result.sign_mismatches:
        log.warning(
            "%d transactions where Pluggy type and amount sign disagree", result.sign_mismatches
        )
    return result
