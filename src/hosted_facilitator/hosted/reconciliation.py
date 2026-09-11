from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.storage import SettlementRecord, SettlementStatus, utcnow


class ReconciliationStatus(StrEnum):
    SETTLED = "settled"
    FAILED_FINAL = "failed_final"
    UNKNOWN = "unknown"
    MANUAL_REVIEW = "manual_review"
    PENDING = "pending"


@dataclass
class ReconciliationOutcome:
    status: ReconciliationStatus
    transaction: str | None = None
    payer: str | None = None
    error_reason: str | None = None


@dataclass
class ReconciliationRunStats:
    scanned: int = 0
    claimed: int = 0
    settled: int = 0
    failed: int = 0
    marked_unknown: int = 0
    manual_review: int = 0
    pending: int = 0
    skipped: int = 0
    errors: int = 0


class HostedSettlementReconciler:
    def __init__(
        self,
        *,
        router: ProviderRouter,
        store: Any,
        owner: str,
        lease_seconds: int = 60,
        stale_in_progress_after: timedelta = timedelta(minutes=2),
        stale_submitted_after: timedelta = timedelta(minutes=2),
        stale_unknown_after: timedelta = timedelta(minutes=5),
        manual_review_after: timedelta = timedelta(minutes=30),
        limit: int = 100,
        clock: Callable[[], datetime] = utcnow,
    ):
        if not owner:
            raise ValueError("reconciliation owner is required")
        self._router = router
        self._store = store
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._stale_in_progress_after = stale_in_progress_after
        self._stale_submitted_after = stale_submitted_after
        self._stale_unknown_after = stale_unknown_after
        self._manual_review_after = manual_review_after
        self._limit = limit
        self._clock = clock

    async def run_once(self) -> ReconciliationRunStats:
        stats = ReconciliationRunStats()
        now = self._clock()
        candidates = self._scan_candidates(now)
        stats.scanned = len(candidates)
        for record in candidates:
            claimed = self._claim(record, now)
            if claimed is None:
                stats.skipped += 1
                continue
            stats.claimed += 1
            provider = self._router.provider_by_name(claimed.provider)
            if provider is None:
                self._mark_manual_or_pending(
                    claimed,
                    now=now,
                    stats=stats,
                    error_reason="reconciliation_provider_not_registered",
                )
                continue
            try:
                outcome = await _reconcile_with_provider(provider, claimed)
            except Exception:
                stats.errors += 1
                if claimed.status == SettlementStatus.SETTLE_IN_PROGRESS:
                    self._try_mark_unknown_after_provider_error(claimed, stats=stats)
                continue
            try:
                self._apply_outcome(claimed, outcome, now=now, stats=stats)
            except Exception:
                stats.errors += 1
        return stats

    def _scan_candidates(self, now: datetime) -> list[SettlementRecord]:
        list_candidates = _required_method(self._store, "list_reconciliation_candidates")
        if self._limit <= 0:
            return []
        scan_specs = (
            (SettlementStatus.SETTLE_IN_PROGRESS, now - self._stale_in_progress_after),
            (SettlementStatus.SUBMITTED, now - self._stale_submitted_after),
            (SettlementStatus.UNKNOWN, now - self._stale_unknown_after),
        )
        buckets = [
            list_candidates(statuses=(status,), stale_before=stale_before, limit=self._limit)
            for status, stale_before in scan_specs
        ]
        records: list[SettlementRecord] = []
        for index in range(self._limit):
            made_progress = False
            for bucket in buckets:
                if index < len(bucket):
                    records.append(bucket[index])
                    made_progress = True
                    if len(records) >= self._limit:
                        return records
            if not made_progress:
                break
        return records

    def _claim(self, record: SettlementRecord, now: datetime) -> SettlementRecord | None:
        if record.record_id is None:
            return None
        claim_record = _required_method(self._store, "claim_reconciliation_record")
        stale_before = self._stale_before_for_status(record.status, now)
        return claim_record(
            record_id=record.record_id,
            owner=self._owner,
            lease_until=now + timedelta(seconds=self._lease_seconds),
            eligible_statuses=(record.status,),
            stale_before=stale_before,
        )

    def _stale_before_for_status(self, status: SettlementStatus, now: datetime) -> datetime:
        if status == SettlementStatus.SETTLE_IN_PROGRESS:
            return now - self._stale_in_progress_after
        if status == SettlementStatus.SUBMITTED:
            return now - self._stale_submitted_after
        return now - self._stale_unknown_after

    def _apply_outcome(
        self,
        record: SettlementRecord,
        outcome: ReconciliationOutcome,
        *,
        now: datetime,
        stats: ReconciliationRunStats,
    ) -> None:
        if outcome.status == ReconciliationStatus.SETTLED:
            transaction = outcome.transaction or record.transaction
            if not transaction:
                raise ValueError("settled reconciliation outcome requires transaction proof")
            mark_settled = _required_method(self._store, "mark_reconciled_settled")
            mark_settled(
                record,
                transaction=transaction,
                payer=outcome.payer or record.payer,
                owner=self._owner,
            )
            stats.settled += 1
            return
        if outcome.status == ReconciliationStatus.FAILED_FINAL:
            if record.status == SettlementStatus.SETTLE_IN_PROGRESS:
                self._mark_unknown(
                    record,
                    stats=stats,
                    error_reason=outcome.error_reason or _default_unknown_reason(record),
                )
                return
            mark_failed = _required_method(self._store, "mark_reconciled_failed")
            mark_failed(record, error_reason=outcome.error_reason, owner=self._owner)
            stats.failed += 1
            return
        if outcome.status == ReconciliationStatus.UNKNOWN:
            if record.status in {SettlementStatus.SUBMITTED, SettlementStatus.SETTLE_IN_PROGRESS}:
                mark_unknown = _required_method(self._store, "mark_reconciled_unknown")
                mark_unknown(
                    record,
                    error_reason=outcome.error_reason or _default_unknown_reason(record),
                    owner=self._owner,
                )
                stats.marked_unknown += 1
                return
            self._mark_manual_or_pending(
                record,
                now=now,
                stats=stats,
                error_reason=outcome.error_reason,
            )
            return
        if outcome.status == ReconciliationStatus.MANUAL_REVIEW:
            self._mark_manual_or_pending(
                record,
                now=now + self._manual_review_after,
                stats=stats,
                error_reason=outcome.error_reason,
            )
            return
        self._mark_manual_or_pending(
            record,
            now=now,
            stats=stats,
            error_reason=outcome.error_reason,
        )

    def _mark_manual_or_pending(
        self,
        record: SettlementRecord,
        *,
        now: datetime,
        stats: ReconciliationRunStats,
        error_reason: str | None,
    ) -> None:
        if record.status == SettlementStatus.SETTLE_IN_PROGRESS:
            self._mark_unknown(
                record,
                stats=stats,
                error_reason=error_reason or _default_unknown_reason(record),
            )
            return
        if (
            record.status == SettlementStatus.UNKNOWN
            and now - record.updated_at >= self._manual_review_after
        ):
            mark_manual = _required_method(self._store, "mark_manual_review")
            mark_manual(record, error_reason=error_reason, owner=self._owner)
            stats.manual_review += 1
            return
        stats.pending += 1

    def _mark_unknown(
        self,
        record: SettlementRecord,
        *,
        stats: ReconciliationRunStats,
        error_reason: str | None,
    ) -> None:
        mark_unknown = _required_method(self._store, "mark_reconciled_unknown")
        mark_unknown(
            record,
            error_reason=error_reason or _default_unknown_reason(record),
            owner=self._owner,
        )
        stats.marked_unknown += 1

    def _try_mark_unknown_after_provider_error(
        self,
        record: SettlementRecord,
        *,
        stats: ReconciliationRunStats,
    ) -> None:
        try:
            self._mark_unknown(
                record,
                stats=stats,
                error_reason="provider_reconciliation_exception",
            )
        except Exception:
            stats.errors += 1


async def _reconcile_with_provider(
    provider: object, record: SettlementRecord
) -> ReconciliationOutcome:
    reconcile = getattr(provider, "reconcile_settlement", None)
    if reconcile is None:
        return ReconciliationOutcome(
            status=ReconciliationStatus.PENDING,
            error_reason="provider_reconciliation_unavailable",
        )
    outcome = reconcile(record)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if isinstance(outcome, ReconciliationOutcome):
        return outcome
    if isinstance(outcome, dict):
        return ReconciliationOutcome(
            status=ReconciliationStatus(outcome["status"]),
            transaction=outcome.get("transaction"),
            payer=outcome.get("payer"),
            error_reason=outcome.get("errorReason") or outcome.get("error_reason"),
        )
    raise TypeError("provider reconcile_settlement must return ReconciliationOutcome or dict")


def _required_method(target: object, name: str):
    method = getattr(target, name, None)
    if method is None:
        raise RuntimeError(f"settlement store does not support {name}")
    return method


def _default_unknown_reason(record: SettlementRecord) -> str:
    if record.status == SettlementStatus.SETTLE_IN_PROGRESS:
        return "stale_settle_in_progress"
    return "reconciliation_unknown"
