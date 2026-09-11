from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol

_USDC_ASSET = "0x3600000000000000000000000000000000000000"


class SettlementStatus(StrEnum):
    RECEIVED = "received"
    SETTLE_IN_PROGRESS = "settle_in_progress"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    SETTLE_FAILED = "settle_failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"
    MANUAL_REVIEW = "manual_review"


class SettlementClaim(StrEnum):
    CLAIMED = "claimed"
    DUPLICATE_IN_FLIGHT = "duplicate_in_flight"
    DUPLICATE_FINAL = "duplicate_final"
    DUPLICATE_UNKNOWN = "duplicate_unknown"
    DUPLICATE_MANUAL_REVIEW = "duplicate_manual_review"


class SettlementAttemptStatus(StrEnum):
    STARTED = "started"
    SUBMITTED = "submitted"
    SETTLED = "settled"
    FAILED = "failed"
    UNKNOWN = "unknown"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SettlementRecord:
    record_id: int | None
    seller_account_id: str
    fingerprint: str
    provider: str
    scheme: str | None
    network: str | None
    status: SettlementStatus = SettlementStatus.RECEIVED
    trace_id: str | None = None
    transaction: str | None = None
    payer: str | None = None
    error_reason: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    raw_requirements: dict[str, Any] = field(default_factory=dict)
    reconciliation_owner: str | None = None
    reconciliation_lease_until: datetime | None = None
    reconciliation_attempts: int = 0
    payment_profile_id: str = "default"
    duplicate_claims: int = 0
    last_duplicate_at: datetime | None = None


@dataclass
class SettlementAttemptRecord:
    attempt_id: int
    settlement_record_id: int
    trace_id: str | None
    status: SettlementAttemptStatus
    started_at: datetime
    finished_at: datetime | None = None
    transaction: str | None = None
    error_reason: str | None = None


class SettlementStore(Protocol):
    durable: bool
    hosted_safe: bool

    def get(
        self,
        seller_account_id: str,
        fingerprint: str,
        *,
        payment_profile_id: str = "default",
    ) -> SettlementRecord | None: ...

    def get_by_provider_fingerprint(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        provider: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> SettlementRecord | None: ...

    def count(self) -> int: ...

    def settlement_status_counts(self) -> dict[str, int]: ...

    def settlement_provider_network_status_counts(self) -> list[dict[str, Any]]: ...

    def settlement_oldest_age_seconds(self) -> dict[str, int]: ...

    def list_recent_records(self, *, limit: int = 25) -> list[SettlementRecord]: ...

    def list_recent_records_for_seller(
        self, seller_account_id: str, *, limit: int = 25
    ) -> list[SettlementRecord]: ...

    def list_reconciliation_queue(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
        limit: int = 50,
    ) -> list[SettlementRecord]: ...

    def reconciliation_queue_status_counts(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
    ) -> dict[str, int]: ...

    def settlement_status_counts_for_seller(self, seller_account_id: str) -> dict[str, int]: ...

    def settlement_attempt_count_for_seller(self, seller_account_id: str) -> int: ...

    def settlement_amount_atomic_for_seller(self, seller_account_id: str) -> int: ...

    def last_successful_settlement_at_for_seller(
        self, seller_account_id: str
    ) -> datetime | None: ...

    def get_record_by_id(self, record_id: int) -> SettlementRecord | None: ...

    def list_attempts_for_record(
        self, record_id: int, *, limit: int = 25
    ) -> list[SettlementAttemptRecord]: ...

    def health_check(self) -> bool: ...

    def attempt_count(self) -> int: ...

    def claim_for_settlement(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str,
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
    ) -> tuple[SettlementRecord, SettlementClaim]: ...

    def claim_and_start_settle_attempt(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str,
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
    ) -> tuple[SettlementRecord, SettlementClaim, int | None]: ...

    def start_settle_attempt(self, record: SettlementRecord, *, trace_id: str) -> int: ...

    def finish_settle_attempt(
        self,
        attempt_id: int,
        *,
        status: SettlementAttemptStatus,
        transaction: str | None = None,
        error_reason: str | None = None,
    ) -> None: ...

    def mark_settled(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None: ...

    def mark_settle_failed(self, record: SettlementRecord, *, error_reason: str | None) -> None: ...

    def mark_submitted(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None: ...

    def mark_unknown(self, record: SettlementRecord, *, error_reason: str | None) -> None: ...

    def claim_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
        lease_until: datetime,
        eligible_statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
    ) -> SettlementRecord | None: ...

    def release_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
    ) -> SettlementRecord | None: ...

    def mark_manual_review(
        self,
        record: SettlementRecord,
        *,
        error_reason: str | None,
        owner: str,
    ) -> None: ...

    def apply_reconciliation_control_action(
        self,
        *,
        action: str,
        record_id: int,
        owner: str,
        lease_until: datetime,
        active_stale_before: datetime,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> tuple[SettlementRecord, object]: ...


class InMemorySettlementStore:
    """Process-local store for alpha tests and local development."""

    durable = False
    hosted_safe = False

    def __init__(self):
        self._records: dict[tuple[str, str, str], SettlementRecord] = {}
        self._provider_fingerprint_index: dict[
            tuple[str, str, str | None, str | None, str], tuple[str, str, str]
        ] = {}
        self._attempts: dict[int, dict[str, Any]] = {}
        self._next_record_id = 1
        self._next_attempt_id = 1
        self._lock = RLock()

    def get(
        self,
        seller_account_id: str,
        fingerprint: str,
        *,
        payment_profile_id: str = "default",
    ) -> SettlementRecord | None:
        with self._lock:
            return self._records.get((seller_account_id, payment_profile_id, fingerprint))

    def get_by_provider_fingerprint(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        provider: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> SettlementRecord | None:
        with self._lock:
            record_key = self._provider_fingerprint_index.get(
                (seller_account_id, provider, scheme, network, fingerprint)
            )
            return self._records.get(record_key) if record_key is not None else None

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def settlement_status_counts(self) -> dict[str, int]:
        with self._lock:
            counts = {status.value: 0 for status in SettlementStatus}
            for record in self._records.values():
                counts[record.status.value] += 1
            return {status: count for status, count in counts.items() if count > 0}

    def settlement_provider_network_status_counts(self) -> list[dict[str, Any]]:
        with self._lock:
            counts: dict[tuple[str, str, str], int] = {}
            for record in self._records.values():
                key = (record.provider, record.network or "", record.status.value)
                counts[key] = counts.get(key, 0) + 1
            return [
                {"provider": provider, "network": network, "status": status, "count": count}
                for (provider, network, status), count in sorted(counts.items())
            ]

    def settlement_oldest_age_seconds(self) -> dict[str, int]:
        with self._lock:
            now = utcnow()
            ages: dict[str, int] = {}
            for record in self._records.values():
                age = max(0, int((now - record.updated_at).total_seconds()))
                status = record.status.value
                ages[status] = max(ages.get(status, 0), age)
            return ages

    def list_recent_records(self, *, limit: int = 25) -> list[SettlementRecord]:
        with self._lock:
            bounded_limit = max(0, min(int(limit), 100))
            return sorted(
                self._records.values(),
                key=lambda record: (record.updated_at, record.record_id or 0),
                reverse=True,
            )[:bounded_limit]

    def list_recent_records_for_seller(
        self, seller_account_id: str, *, limit: int = 25
    ) -> list[SettlementRecord]:
        with self._lock:
            bounded_limit = max(0, min(int(limit), 100))
            return sorted(
                (
                    record
                    for record in self._records.values()
                    if record.seller_account_id == seller_account_id
                ),
                key=lambda record: (record.updated_at, record.record_id or 0),
                reverse=True,
            )[:bounded_limit]

    def list_reconciliation_queue(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
        limit: int = 50,
    ) -> list[SettlementRecord]:
        if not statuses:
            return []
        with self._lock:
            allowed = set(statuses)
            records = [
                record
                for record in self._records.values()
                if record.status in allowed
                and (stale_before is None or record.updated_at <= stale_before)
                and (seller_account_id is None or record.seller_account_id == seller_account_id)
                and (provider is None or record.provider == provider)
                and (network is None or record.network == network)
            ]
            return sorted(records, key=lambda record: (record.updated_at, record.record_id or 0))[
                : max(0, min(int(limit), 100))
            ]

    def settlement_status_counts_for_seller(self, seller_account_id: str) -> dict[str, int]:
        with self._lock:
            counts = {status.value: 0 for status in SettlementStatus}
            for record in self._records.values():
                if record.seller_account_id == seller_account_id:
                    counts[record.status.value] += 1
            return {status: count for status, count in counts.items() if count > 0}

    def reconciliation_queue_status_counts(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
    ) -> dict[str, int]:
        if not statuses:
            return {}
        with self._lock:
            allowed = set(statuses)
            counts: dict[str, int] = {}
            for record in self._records.values():
                if (
                    record.status in allowed
                    and (stale_before is None or record.updated_at <= stale_before)
                    and (seller_account_id is None or record.seller_account_id == seller_account_id)
                    and (provider is None or record.provider == provider)
                    and (network is None or record.network == network)
                ):
                    counts[record.status.value] = counts.get(record.status.value, 0) + 1
            return counts

    def settlement_attempt_count_for_seller(self, seller_account_id: str) -> int:
        with self._lock:
            record_ids = {
                record.record_id
                for record in self._records.values()
                if record.seller_account_id == seller_account_id
            }
            return sum(
                1
                for attempt in self._attempts.values()
                if attempt["settlement_record_id"] in record_ids
            )

    def settlement_amount_atomic_for_seller(self, seller_account_id: str) -> int:
        with self._lock:
            return sum(
                _amount_atomic_from_requirements(record.raw_requirements)
                for record in self._records.values()
                if record.seller_account_id == seller_account_id
                and record.status in {SettlementStatus.SETTLED, SettlementStatus.RECONCILED}
                and _asset_from_requirements(record.raw_requirements) == _USDC_ASSET
            )

    def last_successful_settlement_at_for_seller(self, seller_account_id: str) -> datetime | None:
        with self._lock:
            successful = [
                record.updated_at
                for record in self._records.values()
                if record.seller_account_id == seller_account_id
                and record.status in {SettlementStatus.SETTLED, SettlementStatus.RECONCILED}
            ]
            return max(successful) if successful else None

    def get_record_by_id(self, record_id: int) -> SettlementRecord | None:
        with self._lock:
            for record in self._records.values():
                if record.record_id == record_id:
                    return record
            return None

    def list_attempts_for_record(
        self, record_id: int, *, limit: int = 25
    ) -> list[SettlementAttemptRecord]:
        with self._lock:
            bounded_limit = max(0, min(int(limit), 100))
            attempts = [
                SettlementAttemptRecord(
                    attempt_id=attempt_id,
                    settlement_record_id=int(attempt["settlement_record_id"] or 0),
                    trace_id=attempt["trace_id"],
                    status=attempt["status"],
                    started_at=attempt["started_at"],
                    finished_at=attempt["finished_at"],
                    transaction=attempt["transaction"],
                    error_reason=attempt["error_reason"],
                )
                for attempt_id, attempt in self._attempts.items()
                if int(attempt["settlement_record_id"] or 0) == record_id
            ]
            return sorted(attempts, key=lambda attempt: attempt.started_at, reverse=True)[
                :bounded_limit
            ]

    def health_check(self) -> bool:
        return True

    def attempt_count(self) -> int:
        with self._lock:
            return len(self._attempts)

    def claim_for_settlement(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
    ) -> tuple[SettlementRecord, SettlementClaim]:
        with self._lock:
            key = (seller_account_id, payment_profile_id, fingerprint)
            existing = self._records.get(key)
            if existing is not None:
                _record_duplicate_claim(existing)
                return existing, _claim_for_status(existing.status)
            replay_key = (
                seller_account_id,
                provider,
                scheme,
                network,
                fingerprint,
            )
            replay_record_key = self._provider_fingerprint_index.get(replay_key)
            if replay_record_key is not None:
                replay_record = self._records[replay_record_key]
                _record_duplicate_claim(replay_record)
                return replay_record, _claim_for_status(replay_record.status)
            record = SettlementRecord(
                record_id=self._next_record_id,
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
                fingerprint=fingerprint,
                provider=provider,
                scheme=scheme,
                network=network,
                status=SettlementStatus.SETTLE_IN_PROGRESS,
                trace_id=trace_id,
                raw_requirements=_safe_requirements(raw_requirements),
            )
            self._next_record_id += 1
            self._records[key] = record
            self._provider_fingerprint_index[replay_key] = key
            return record, SettlementClaim.CLAIMED

    def start_settle_attempt(self, record: SettlementRecord, *, trace_id: str) -> int:
        with self._lock:
            attempt_id = self._next_attempt_id
            self._next_attempt_id += 1
            self._attempts[attempt_id] = {
                "settlement_record_id": record.record_id,
                "trace_id": trace_id,
                "status": SettlementAttemptStatus.STARTED,
                "started_at": utcnow(),
                "finished_at": None,
                "transaction": None,
                "error_reason": None,
            }
            return attempt_id

    def finish_settle_attempt(
        self,
        attempt_id: int,
        *,
        status: SettlementAttemptStatus,
        transaction: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None:
                return
            attempt["status"] = status
            attempt["finished_at"] = utcnow()
            attempt["transaction"] = transaction
            attempt["error_reason"] = error_reason

    def mark_settled(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        with self._lock:
            record.status = SettlementStatus.SETTLED
            record.transaction = transaction
            record.payer = payer
            record.updated_at = utcnow()

    def mark_settle_failed(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        with self._lock:
            record.status = SettlementStatus.SETTLE_FAILED
            record.error_reason = error_reason
            record.updated_at = utcnow()

    def mark_submitted(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        with self._lock:
            record.status = SettlementStatus.SUBMITTED
            record.transaction = transaction
            record.payer = payer
            record.updated_at = utcnow()

    def mark_unknown(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        with self._lock:
            record.status = SettlementStatus.UNKNOWN
            record.error_reason = error_reason
            record.updated_at = utcnow()


class SqliteSettlementStore:
    """Durable local store that mirrors the hosted settlement-state contract."""

    durable = True
    hosted_safe = False

    def __init__(self, path: str):
        self._path = path
        self._lock = RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._conn.close()

    def health_check(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
        return True

    def _initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS settlement_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_account_id TEXT NOT NULL,
                    payment_profile_id TEXT NOT NULL DEFAULT 'default',
                    fingerprint TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    scheme TEXT,
                    network TEXT,
                    status TEXT NOT NULL,
                    trace_id TEXT,
                    transaction_hash TEXT,
                    payer TEXT,
                    error_reason TEXT,
                    raw_requirements_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    duplicate_claims INTEGER NOT NULL DEFAULT 0,
                    last_duplicate_at TEXT,
                    UNIQUE(seller_account_id, payment_profile_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_settlement_records_seller_profile
                    ON settlement_records(seller_account_id, payment_profile_id);
                CREATE INDEX IF NOT EXISTS idx_settlement_records_status_updated
                    ON settlement_records(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_settlement_records_trace_id
                    ON settlement_records(trace_id);
                CREATE INDEX IF NOT EXISTS idx_settlement_records_provider_network_status
                    ON settlement_records(provider, network, status);

                CREATE TABLE IF NOT EXISTS settlement_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    settlement_record_id INTEGER NOT NULL,
                    trace_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    transaction_hash TEXT,
                    error_reason TEXT,
                    FOREIGN KEY(settlement_record_id) REFERENCES settlement_records(id)
                );
                CREATE INDEX IF NOT EXISTS idx_settlement_attempts_record_started
                    ON settlement_attempts(settlement_record_id, started_at);
                """
            )
            for statement in (
                "ALTER TABLE settlement_records ADD COLUMN duplicate_claims INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE settlement_records ADD COLUMN last_duplicate_at TEXT",
            ):
                try:
                    self._conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def get(
        self,
        seller_account_id: str,
        fingerprint: str,
        *,
        payment_profile_id: str = "default",
    ) -> SettlementRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM settlement_records
                WHERE seller_account_id = ? AND payment_profile_id = ? AND fingerprint = ?
                """,
                (seller_account_id, payment_profile_id, fingerprint),
            ).fetchone()
            return _record_from_row(row) if row is not None else None

    def get_by_provider_fingerprint(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        provider: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> SettlementRecord | None:
        with self._lock:
            row = self._select_provider_fingerprint_record(
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
                provider=provider,
                scheme=scheme,
                network=network,
                fingerprint=fingerprint,
            )
            return _record_from_row(row) if row is not None else None

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM settlement_records").fetchone()
            return int(row["count"])

    def settlement_status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM settlement_records
                GROUP BY status
                """
            ).fetchall()
            return {
                SettlementStatus(row["status"]).value: int(row["count"])
                for row in rows
                if row["status"] in {status.value for status in SettlementStatus}
            }

    def settlement_provider_network_status_counts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT provider, COALESCE(network, '') AS network, status, COUNT(*) AS count
                FROM settlement_records
                GROUP BY provider, COALESCE(network, ''), status
                ORDER BY provider, network, status
                """
            ).fetchall()
            allowed = {status.value for status in SettlementStatus}
            return [
                {
                    "provider": str(row["provider"]),
                    "network": str(row["network"]),
                    "status": SettlementStatus(row["status"]).value,
                    "count": int(row["count"]),
                }
                for row in rows
                if row["status"] in allowed
            ]

    def settlement_oldest_age_seconds(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, updated_at
                FROM settlement_records
                """
            ).fetchall()
            now = utcnow()
            ages: dict[str, int] = {}
            for row in rows:
                status = str(row["status"])
                if status not in {item.value for item in SettlementStatus}:
                    continue
                updated_at = _parse_datetime(str(row["updated_at"]))
                age = max(0, int((now - updated_at).total_seconds()))
                ages[status] = max(ages.get(status, 0), age)
            return ages

    def list_recent_records(self, *, limit: int = 25) -> list[SettlementRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM settlement_records
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(0, min(int(limit), 100)),),
            ).fetchall()
            return [_record_from_row(row) for row in rows]

    def list_recent_records_for_seller(
        self, seller_account_id: str, *, limit: int = 25
    ) -> list[SettlementRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM settlement_records
                WHERE seller_account_id = ?
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (seller_account_id, max(0, min(int(limit), 100))),
            ).fetchall()
            return [_record_from_row(row) for row in rows]

    def list_reconciliation_queue(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
        limit: int = 50,
    ) -> list[SettlementRecord]:
        if not statuses:
            return []
        status_placeholders = ", ".join("?" for _ in statuses)
        clauses = [f"status IN ({status_placeholders})"]
        params: list[Any] = [status.value for status in statuses]
        if stale_before is not None:
            clauses.append("updated_at <= ?")
            params.append(_serialize_datetime(stale_before))
        if seller_account_id is not None:
            clauses.append("seller_account_id = ?")
            params.append(seller_account_id)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if network is not None:
            clauses.append("network = ?")
            params.append(network)
        params.append(max(0, min(int(limit), 100)))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT *
                FROM settlement_records
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [_record_from_row(row) for row in rows]

    def reconciliation_queue_status_counts(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
    ) -> dict[str, int]:
        if not statuses:
            return {}
        status_placeholders = ", ".join("?" for _ in statuses)
        clauses = [f"status IN ({status_placeholders})"]
        params: list[Any] = [status.value for status in statuses]
        if stale_before is not None:
            clauses.append("updated_at <= ?")
            params.append(_serialize_datetime(stale_before))
        if seller_account_id is not None:
            clauses.append("seller_account_id = ?")
            params.append(seller_account_id)
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if network is not None:
            clauses.append("network = ?")
            params.append(network)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT status, COUNT(*) AS count
                FROM settlement_records
                WHERE {" AND ".join(clauses)}
                GROUP BY status
                """,
                tuple(params),
            ).fetchall()
            allowed = {status.value for status in SettlementStatus}
            return {
                SettlementStatus(row["status"]).value: int(row["count"])
                for row in rows
                if row["status"] in allowed
            }

    def settlement_status_counts_for_seller(self, seller_account_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM settlement_records
                WHERE seller_account_id = ?
                GROUP BY status
                """,
                (seller_account_id,),
            ).fetchall()
            allowed = {status.value for status in SettlementStatus}
            return {
                SettlementStatus(row["status"]).value: int(row["count"])
                for row in rows
                if row["status"] in allowed
            }

    def settlement_attempt_count_for_seller(self, seller_account_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM settlement_attempts attempts
                JOIN settlement_records records ON records.id = attempts.settlement_record_id
                WHERE records.seller_account_id = ?
                """,
                (seller_account_id,),
            ).fetchone()
            return int(row["count"])

    def settlement_amount_atomic_for_seller(self, seller_account_id: str) -> int:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT raw_requirements_json
                FROM settlement_records
                WHERE seller_account_id = ?
                  AND status IN (?, ?)
                """,
                (
                    seller_account_id,
                    SettlementStatus.SETTLED.value,
                    SettlementStatus.RECONCILED.value,
                ),
            ).fetchall()
            total = 0
            for row in rows:
                try:
                    requirements = json.loads(row["raw_requirements_json"])
                    if _asset_from_requirements(requirements) == _USDC_ASSET:
                        total += _amount_atomic_from_requirements(requirements)
                except Exception:
                    continue
            return total

    def last_successful_settlement_at_for_seller(self, seller_account_id: str) -> datetime | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MAX(updated_at) AS last_settlement_at
                FROM settlement_records
                WHERE seller_account_id = ?
                  AND status IN (?, ?)
                """,
                (
                    seller_account_id,
                    SettlementStatus.SETTLED.value,
                    SettlementStatus.RECONCILED.value,
                ),
            ).fetchone()
            value = row["last_settlement_at"]
            return _parse_datetime(value) if value else None

    def get_record_by_id(self, record_id: int) -> SettlementRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM settlement_records
                WHERE id = ?
                """,
                (int(record_id),),
            ).fetchone()
            return _record_from_row(row) if row is not None else None

    def list_attempts_for_record(
        self, record_id: int, *, limit: int = 25
    ) -> list[SettlementAttemptRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM settlement_attempts
                WHERE settlement_record_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (int(record_id), max(0, min(int(limit), 100))),
            ).fetchall()
            return [_attempt_from_row(row) for row in rows]

    def attempt_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM settlement_attempts").fetchone()
            return int(row["count"])

    def claim_for_settlement(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
    ) -> tuple[SettlementRecord, SettlementClaim]:
        with self._lock:
            now = _serialize_datetime(utcnow())
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._select_record(seller_account_id, payment_profile_id, fingerprint)
                if existing is not None:
                    self._increment_duplicate_claim(existing)
                    self._conn.execute("COMMIT")
                    record = self.get_record_by_id(int(existing["id"]))
                    if record is None:
                        raise RuntimeError("SQLite duplicate settlement record disappeared")
                    return record, _claim_for_status(record.status)
                replay = self._select_provider_fingerprint_record(
                    seller_account_id=seller_account_id,
                    payment_profile_id=payment_profile_id,
                    provider=provider,
                    scheme=scheme,
                    network=network,
                    fingerprint=fingerprint,
                )
                if replay is not None:
                    self._increment_duplicate_claim(replay)
                    self._conn.execute("COMMIT")
                    record = self.get_record_by_id(int(replay["id"]))
                    if record is None:
                        raise RuntimeError("SQLite duplicate settlement record disappeared")
                    return record, _claim_for_status(record.status)
                cursor = self._conn.execute(
                    """
                    INSERT INTO settlement_records (
                        seller_account_id, payment_profile_id, fingerprint, provider, scheme,
                        network, status, trace_id, raw_requirements_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        seller_account_id,
                        payment_profile_id,
                        fingerprint,
                        provider,
                        scheme,
                        network,
                        SettlementStatus.SETTLE_IN_PROGRESS,
                        trace_id,
                        json.dumps(_safe_requirements(raw_requirements), sort_keys=True),
                        now,
                        now,
                    ),
                )
                record = SettlementRecord(
                    record_id=int(cursor.lastrowid),
                    seller_account_id=seller_account_id,
                    payment_profile_id=payment_profile_id,
                    fingerprint=fingerprint,
                    provider=provider,
                    scheme=scheme,
                    network=network,
                    status=SettlementStatus.SETTLE_IN_PROGRESS,
                    trace_id=trace_id,
                    created_at=_parse_datetime(now),
                    updated_at=_parse_datetime(now),
                    raw_requirements=_safe_requirements(raw_requirements),
                )
                self._conn.execute("COMMIT")
                return record, SettlementClaim.CLAIMED
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def start_settle_attempt(self, record: SettlementRecord, *, trace_id: str) -> int:
        if record.record_id is None:
            raise ValueError("settlement record id is required for attempts")
        with self._lock:
            now = _serialize_datetime(utcnow())
            cursor = self._conn.execute(
                """
                INSERT INTO settlement_attempts (
                    settlement_record_id, trace_id, status, started_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (record.record_id, trace_id, SettlementAttemptStatus.STARTED, now),
            )
            return int(cursor.lastrowid)

    def finish_settle_attempt(
        self,
        attempt_id: int,
        *,
        status: SettlementAttemptStatus,
        transaction: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE settlement_attempts
                SET status = ?, finished_at = ?, transaction_hash = ?, error_reason = ?
                WHERE id = ?
                """,
                (status, _serialize_datetime(utcnow()), transaction, error_reason, attempt_id),
            )

    def mark_settled(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SETTLED,
            transaction=transaction,
            payer=payer,
            error_reason=None,
        )

    def mark_settle_failed(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SETTLE_FAILED,
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
        )

    def mark_submitted(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SUBMITTED,
            transaction=transaction,
            payer=payer,
            error_reason=None,
        )

    def mark_unknown(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        self._update_record(
            record,
            status=SettlementStatus.UNKNOWN,
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
        )

    def _select_record(
        self, seller_account_id: str, payment_profile_id: str, fingerprint: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM settlement_records
            WHERE seller_account_id = ? AND payment_profile_id = ? AND fingerprint = ?
            """,
            (seller_account_id, payment_profile_id, fingerprint),
        ).fetchone()

    def _select_provider_fingerprint_record(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM settlement_records
            WHERE seller_account_id = ?
              AND provider = ?
              AND COALESCE(scheme, '') = COALESCE(?, '')
              AND COALESCE(network, '') = COALESCE(?, '')
              AND fingerprint = ?
            LIMIT 1
            """,
            (seller_account_id, provider, scheme, network, fingerprint),
        ).fetchone()

    def _increment_duplicate_claim(self, row: sqlite3.Row) -> None:
        self._conn.execute(
            """
            UPDATE settlement_records
            SET duplicate_claims = duplicate_claims + 1,
                last_duplicate_at = ?
            WHERE id = ?
            """,
            (_serialize_datetime(utcnow()), int(row["id"])),
        )

    def _update_record(
        self,
        record: SettlementRecord,
        *,
        status: SettlementStatus,
        transaction: str | None,
        payer: str | None,
        error_reason: str | None,
    ) -> None:
        with self._lock:
            updated_at = utcnow()
            if record.record_id is None:
                raise ValueError("settlement record id is required for updates")
            self._conn.execute(
                """
                UPDATE settlement_records
                SET status = ?, transaction_hash = ?, payer = ?, error_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    transaction,
                    payer,
                    error_reason,
                    _serialize_datetime(updated_at),
                    record.record_id,
                ),
            )
            record.status = status
            record.transaction = transaction
            record.payer = payer
            record.error_reason = error_reason
            record.updated_at = updated_at


def _claim_for_status(status: SettlementStatus) -> SettlementClaim:
    if status in {SettlementStatus.SETTLED, SettlementStatus.SETTLE_FAILED}:
        return SettlementClaim.DUPLICATE_FINAL
    if status in {
        SettlementStatus.UNKNOWN,
        SettlementStatus.SUBMITTED,
        SettlementStatus.RECONCILED,
    }:
        return SettlementClaim.DUPLICATE_UNKNOWN
    if status == SettlementStatus.MANUAL_REVIEW:
        return SettlementClaim.DUPLICATE_MANUAL_REVIEW
    return SettlementClaim.DUPLICATE_IN_FLIGHT


def _record_duplicate_claim(record: SettlementRecord) -> None:
    record.duplicate_claims += 1
    record.last_duplicate_at = utcnow()


def _record_from_row(row: sqlite3.Row) -> SettlementRecord:
    row_keys = set(row.keys())
    return SettlementRecord(
        record_id=int(row["id"]),
        seller_account_id=row["seller_account_id"],
        payment_profile_id=(
            row["payment_profile_id"] if "payment_profile_id" in row_keys else "default"
        ),
        fingerprint=row["fingerprint"],
        provider=row["provider"],
        scheme=row["scheme"],
        network=row["network"],
        status=SettlementStatus(row["status"]),
        trace_id=row["trace_id"],
        transaction=row["transaction_hash"],
        payer=row["payer"],
        error_reason=row["error_reason"],
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        raw_requirements=json.loads(row["raw_requirements_json"]),
        reconciliation_owner=(
            row["reconciliation_owner"] if "reconciliation_owner" in row_keys else None
        ),
        reconciliation_lease_until=(
            _parse_datetime(row["reconciliation_lease_until"])
            if "reconciliation_lease_until" in row_keys
            and row["reconciliation_lease_until"] is not None
            else None
        ),
        reconciliation_attempts=(
            int(row["reconciliation_attempts"]) if "reconciliation_attempts" in row_keys else 0
        ),
        duplicate_claims=(int(row["duplicate_claims"]) if "duplicate_claims" in row_keys else 0),
        last_duplicate_at=(
            _parse_datetime(row["last_duplicate_at"])
            if "last_duplicate_at" in row_keys and row["last_duplicate_at"] is not None
            else None
        ),
    )


def _attempt_from_row(row: sqlite3.Row) -> SettlementAttemptRecord:
    return SettlementAttemptRecord(
        attempt_id=int(row["id"]),
        settlement_record_id=int(row["settlement_record_id"]),
        trace_id=row["trace_id"],
        status=SettlementAttemptStatus(row["status"]),
        started_at=_parse_datetime(row["started_at"]),
        finished_at=(
            _parse_datetime(row["finished_at"]) if row["finished_at"] is not None else None
        ),
        transaction=row["transaction_hash"],
        error_reason=row["error_reason"],
    )


def _serialize_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _safe_requirements(requirements: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in ("scheme", "network", "asset", "amount", "payTo", "maxTimeoutSeconds"):
        if key in requirements:
            safe[key] = requirements[key]
    resource = requirements.get("resource")
    if isinstance(resource, str):
        safe["resourceHash"] = hashlib.sha256(resource.encode("utf-8")).hexdigest()
    extra = requirements.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("name"), str):
        safe["extra"] = {"name": extra["name"]}
    safe["metadataRedacted"] = True
    return safe


def _amount_atomic_from_requirements(requirements: dict[str, Any]) -> int:
    amount = str(requirements.get("amount") or "")
    return int(amount) if amount.isdigit() else 0


def _asset_from_requirements(requirements: dict[str, Any]) -> str:
    return str(requirements.get("asset") or "").lower()
