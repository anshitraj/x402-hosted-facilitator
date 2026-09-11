from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

HOSTED_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS hosted_schema_migrations (
    scope TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(scope, version)
);
"""

SELECT_APPLIED_MIGRATIONS_SQL = """
SELECT version, checksum
FROM hosted_schema_migrations
WHERE scope = %s
"""

INSERT_APPLIED_MIGRATION_SQL = """
INSERT INTO hosted_schema_migrations (
    scope, version, description, checksum, applied_at
)
VALUES (%s, %s, %s, %s, %s)
"""

MIGRATION_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


@dataclass(frozen=True)
class HostedPostgresMigration:
    scope: str
    version: str
    description: str
    sql: str
    accepted_checksums: tuple[str, ...] = ()

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def accepts_checksum(self, checksum: str) -> bool:
        return checksum == self.checksum or checksum in self.accepted_checksums


def apply_hosted_postgres_migrations(
    connection: Any,
    *,
    scope: str,
    migrations: Iterable[HostedPostgresMigration],
    validate_schema: Callable[[], bool] | None = None,
) -> None:
    ordered = tuple(migrations)
    if any(migration.scope != scope for migration in ordered):
        raise ValueError(f"All hosted Postgres migrations must use scope {scope!r}")

    try:
        with _transaction(connection):
            _execute(connection, HOSTED_SCHEMA_MIGRATIONS_SQL)
            _execute(connection, MIGRATION_LOCK_SQL, (f"omniclaw.hosted.{scope}.migrations",))
            applied = _applied_migrations(connection, scope)
            migrations_to_record: list[HostedPostgresMigration] = []
            for migration in ordered:
                existing_checksum = applied.get(migration.version)
                if existing_checksum is not None:
                    if not migration.accepts_checksum(existing_checksum):
                        raise RuntimeError(
                            "Hosted Postgres migration checksum mismatch "
                            f"for {scope}:{migration.version}"
                        )
                    continue
                _execute(connection, migration.sql)
                migrations_to_record.append(migration)

            if validate_schema is not None and not validate_schema():
                raise RuntimeError(f"Hosted Postgres schema validation failed for {scope}")

            for migration in migrations_to_record:
                _execute(
                    connection,
                    INSERT_APPLIED_MIGRATION_SQL,
                    (
                        scope,
                        migration.version,
                        migration.description,
                        migration.checksum,
                        datetime.now(timezone.utc),
                    ),
                )
        _commit(connection)
    except Exception:
        _rollback(connection)
        raise


def hosted_postgres_migrations_current(
    connection: Any,
    *,
    scope: str,
    migrations: Iterable[HostedPostgresMigration],
) -> bool:
    expected = {migration.version: migration for migration in migrations}
    applied = _applied_migrations(connection, scope)
    if set(applied) != set(expected):
        return False
    return all(
        expected[version].accepts_checksum(checksum) for version, checksum in applied.items()
    )


def _applied_migrations(connection: Any, scope: str) -> dict[str, str]:
    result = _execute(connection, SELECT_APPLIED_MIGRATIONS_SQL, (scope,))
    if result is None:
        return {}
    rows = result.fetchall()
    applied: dict[str, str] = {}
    for row in rows:
        mapping = dict(row) if row is not None and not isinstance(row, dict) else row
        if mapping is None:
            continue
        applied[str(mapping["version"])] = str(mapping["checksum"])
    return applied


def _transaction(connection: Any):
    transaction = getattr(connection, "transaction", None)
    if transaction is None:
        return nullcontext()
    return transaction()


def _execute(connection: Any, sql: str, params: tuple[Any, ...] | None = None) -> Any:
    return connection.execute(sql, params)


def _commit(connection: Any) -> None:
    commit = getattr(connection, "commit", None)
    if commit is not None:
        commit()


def _rollback(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if rollback is not None:
        rollback()
