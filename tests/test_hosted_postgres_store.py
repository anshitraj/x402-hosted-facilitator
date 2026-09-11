from __future__ import annotations

from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hosted_facilitator.hosted.app import create_hosted_facilitator_app
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import (
    ControlPlaneTargetType,
    _safe_operator_lease_owner,
)
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.limits import RateLimitDecision
from hosted_facilitator.hosted.migrations import (
    HOSTED_SCHEMA_MIGRATIONS_SQL,
    INSERT_APPLIED_MIGRATION_SQL,
    SELECT_APPLIED_MIGRATIONS_SQL,
    HostedPostgresMigration,
    apply_hosted_postgres_migrations,
    hosted_postgres_migrations_current,
)
from hosted_facilitator.hosted.postgres import (
    CLAIM_RECONCILIATION_RECORD_SQL,
    CLAIM_RECORD_SQL,
    INSERT_ATTEMPT_SQL,
    LIST_RECONCILIATION_CANDIDATES_SQL,
    LIST_STALE_STARTED_ATTEMPTS_SQL,
    POSTGRES_CONTROL_PLANE_HEALTH_SQL,
    POSTGRES_CONTROL_PLANE_MIGRATIONS,
    POSTGRES_CONTROL_PLANE_SCHEMA_SQL,
    POSTGRES_CONTROL_PLANE_SELLER_AUDIT_TARGETS_SQL,
    POSTGRES_CONTROL_PLANE_SETTLEMENT_AUDIT_TARGET_SQL,
    POSTGRES_CONTROL_PLANE_TARGET_TYPE_CONSTRAINT_SQL,
    POSTGRES_HEALTH_SQL,
    POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL,
    POSTGRES_SCHEMA_SQL,
    POSTGRES_SELLER_ACCOUNTS_HEALTH_SQL,
    POSTGRES_SELLER_ACCOUNTS_MIGRATIONS,
    POSTGRES_SELLER_ACCOUNTS_SCHEMA_SQL,
    POSTGRES_SELLER_PROJECT_COMPATIBILITY_SQL,
    POSTGRES_SELLER_REVOKE_PROJECT_API_KEYS_SQL,
    POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL,
    POSTGRES_SETTLEMENT_MIGRATIONS,
    POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL,
    POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL,
    POSTGRES_SETTLEMENT_SELLER_ACCOUNTS_SQL,
    PROVIDER_FINGERPRINT_LOCK_SQL,
    PostgresControlPlaneState,
    PostgresSellerAccountResolver,
    PostgresSettlementStore,
    PostgresSettlementStorePool,
    postgres_control_plane_state_from_env,
    postgres_seller_account_resolver_from_env,
    postgres_store_from_env,
)
from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, SettleOutcome, VerifyOutcome
from hosted_facilitator.hosted.storage import SettlementClaim, SettlementStatus, utcnow
from hosted_facilitator.hosted.telemetry import HostedTelemetryConfig
from hosted_facilitator.hosted.tenancy import StaticSellerAccountResolver, seller_key_hash


class _Cursor:
    def __init__(self, rows=None, rowcount=1):
        if rows is None:
            rows = []
        elif isinstance(rows, list):
            rows = rows
        else:
            rows = [rows]
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self):
        rows = list(self._rows)
        self._rows.clear()
        return rows


class _Transaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        self._connection.transactions += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Connection:
    def __init__(self, rows=None, rowcounts=None):
        self.rows = list(rows or [])
        self.rowcounts = list(rowcounts or [])
        self.calls = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 1
        normalized_sql = " ".join(str(sql).split()).upper()
        produces_rows = (
            normalized_sql.startswith("SELECT")
            or normalized_sql.startswith("WITH")
            or " RETURNING " in normalized_sql
        )
        if normalized_sql.startswith("SELECT PG_ADVISORY_XACT_LOCK"):
            produces_rows = False
        if self.rows and produces_rows:
            return _Cursor(self.rows.pop(0), rowcount=rowcount)
        return _Cursor(rowcount=rowcount)

    def transaction(self):
        return _Transaction(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _ClosableConnection(_Connection):
    def __init__(self, rows=None, rowcounts=None):
        super().__init__(rows=rows, rowcounts=rowcounts)
        self.closed = 0

    def close(self):
        self.closed += 1


class _FailingAttemptClosureConnection(_Connection):
    def execute(self, sql, params=None):
        normalized_sql = " ".join(str(sql).split()).upper()
        if normalized_sql.startswith("UPDATE SETTLEMENT_ATTEMPTS"):
            raise RuntimeError("forced attempt closure failure")
        return super().execute(sql, params)


class _Provider(HostedFacilitatorProvider):
    name = "exact_evm"

    async def supported(self, context):
        return []

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context,
    ) -> VerifyOutcome:
        return VerifyOutcome(isValid=True)

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context,
    ) -> SettleOutcome:
        return SettleOutcome(success=True, settlementStatus="settled", transaction="0xpg")


class _HostedSafeRateLimiter:
    hosted_safe = True

    async def check(self, request):
        return RateLimitDecision(
            allowed=True,
            limit=1,
            remaining=0,
            reset_after_seconds=60,
        )


class _HostedSafeProjectResolver(StaticSellerAccountResolver):
    hosted_safe = True


def _row(
    *,
    inserted: bool,
    status: SettlementStatus = SettlementStatus.SETTLE_IN_PROGRESS,
    reconciliation_owner: str | None = None,
    reconciliation_lease_until=None,
    payment_profile_id: str = "default",
):
    now = utcnow()
    return {
        "id": 7,
        "seller_account_id": "default",
        "fingerprint": "fp",
        "provider": "exact_evm",
        "scheme": "exact",
        "network": "eip155:5042002",
        "status": status,
        "trace_id": "tr_pg",
        "transaction_hash": None,
        "payer": None,
        "error_reason": None,
        "reconciliation_owner": reconciliation_owner,
        "reconciliation_lease_until": reconciliation_lease_until,
        "reconciliation_attempts": 0,
        "raw_requirements_json": {"metadataRedacted": True},
        "created_at": now,
        "updated_at": now,
        "payment_profile_id": payment_profile_id,
        "inserted": inserted,
    }


def _project_row(**overrides):
    row = {
        "id": "seller-a",
        "tenant_id": "tenant-a",
        "name": "Seller Account A",
        "environment": "testnet",
        "status": "active",
        "profile_id": "default",
        "profile_name": "Default",
        "profile_status": "active",
        "enabled_networks_json": ["eip155:5042002"],
        "enabled_schemes_json": ["exact"],
        "enabled_providers_json": ["exact_evm"],
        "allowed_assets_json": ["0x3600000000000000000000000000000000000000"],
        "allowed_pay_to_json": ["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"],
        "rate_limits_json": {
            "sellerRequestsPerMinute": 10,
            "supportedPerMinute": 9,
            "verifyPerMinute": 8,
            "settlePerMinute": 7,
            "invalidRequestsPerMinute": 6,
            "ipRequestsPerMinute": 5,
        },
    }
    row.update(overrides)
    return row


def _hosted_otel_config() -> HostedTelemetryConfig:
    return HostedTelemetryConfig(
        environment="production",
        required=True,
        otlp_endpoint="https://otel.example.test",
        collector_health_url="https://otel.example.test/health",
        json_logs_required=True,
    )


def _hosted_oidc_openfga_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", "omniclaw-ops")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_URL", "https://openfga.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", "store-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", "model-id")


def _applied_migration_rows(migrations):
    return [
        {"version": migration.version, "checksum": migration.checksum} for migration in migrations
    ]


def _project_schema_health_row(**overrides):
    row = {
        "hosted_tenants_table": True,
        "hosted_seller_accounts_table": True,
        "hosted_seller_payment_profiles_table": True,
        "hosted_seller_api_keys_table": True,
        "hosted_seller_accounts_tenant_status_index": True,
        "hosted_seller_payment_profiles_seller_status_index": True,
        "hosted_seller_api_keys_seller_status_index": True,
        "hosted_seller_api_keys_profile_status_index": True,
        "hosted_tenants_primary_key": True,
        "hosted_seller_accounts_primary_key": True,
        "hosted_seller_accounts_tenant_foreign_key": True,
        "hosted_seller_payment_profiles_primary_key": True,
        "api_key_hash_primary_key": True,
        "api_key_id_unique": True,
        "api_keys_payment_profile_foreign_key": True,
    }
    row.update(overrides)
    return row


def test_hosted_postgres_migration_runner_applies_and_records_unapplied_migration():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
    )
    connection = _Connection(rows=[[]])

    apply_hosted_postgres_migrations(connection, scope="example", migrations=(migration,))

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert HOSTED_SCHEMA_MIGRATIONS_SQL in rendered_calls
    assert "pg_advisory_xact_lock" in rendered_calls
    assert SELECT_APPLIED_MIGRATIONS_SQL in rendered_calls
    assert migration.sql in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL in rendered_calls
    assert connection.transactions == 1
    assert connection.commits == 1


def test_hosted_postgres_migration_runner_skips_applied_matching_migration():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
    )
    connection = _Connection(rows=[_applied_migration_rows((migration,))])

    apply_hosted_postgres_migrations(connection, scope="example", migrations=(migration,))

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert migration.sql not in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL not in rendered_calls
    assert connection.commits == 1


def test_hosted_postgres_migration_runner_accepts_explicit_recovery_checksums():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
        accepted_checksums=("historical-checksum",),
    )
    connection = _Connection(
        rows=[[{"version": migration.version, "checksum": "historical-checksum"}]]
    )

    apply_hosted_postgres_migrations(connection, scope="example", migrations=(migration,))

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert migration.sql not in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL not in rendered_calls
    assert connection.commits == 1


def test_hosted_postgres_migration_runner_rejects_checksum_drift():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
    )
    connection = _Connection(rows=[[{"version": migration.version, "checksum": "bad-checksum"}]])

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        apply_hosted_postgres_migrations(connection, scope="example", migrations=(migration,))

    assert connection.rollbacks == 1


def test_hosted_postgres_migrations_current_accepts_explicit_recovery_checksums():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
        accepted_checksums=("historical-checksum",),
    )
    connection = _Connection(
        rows=[[{"version": migration.version, "checksum": "historical-checksum"}]]
    )

    assert hosted_postgres_migrations_current(
        connection,
        scope="example",
        migrations=(migration,),
    )


def test_hosted_postgres_migration_runner_validates_schema_before_recording_migration():
    migration = HostedPostgresMigration(
        scope="example",
        version="202605170001",
        description="create example table",
        sql="CREATE TABLE IF NOT EXISTS example_records (id BIGSERIAL PRIMARY KEY);",
    )
    connection = _Connection(rows=[[]])

    with pytest.raises(RuntimeError, match="schema validation failed"):
        apply_hosted_postgres_migrations(
            connection,
            scope="example",
            migrations=(migration,),
            validate_schema=lambda: False,
        )

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert migration.sql in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL not in rendered_calls
    assert connection.rollbacks == 1


def test_postgres_schema_has_required_constraints_and_indexes():
    assert "UNIQUE(seller_account_id, payment_profile_id, fingerprint)" in POSTGRES_SCHEMA_SQL
    assert (
        "CREATE TABLE IF NOT EXISTS settlement_attempts"
        in POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL
    )
    assert (
        "idx_settlement_records_provider_network_status"
        in POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL
    )
    assert "reconciliation_owner TEXT" in POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL
    assert "reconciliation_lease_until TIMESTAMPTZ" in POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL
    assert (
        "idx_settlement_records_reconciliation_lease"
        in POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL
    )
    assert "JSONB NOT NULL" in POSTGRES_SCHEMA_SQL
    assert (
        POSTGRES_SETTLEMENT_MIGRATIONS[0].checksum
        == "7b4ec8b04fba2819b336d30e24a412a9fbdaffd011aa79bb837c8ea03cc4675c"
    )
    assert (
        "duplicate_claims INTEGER NOT NULL DEFAULT 0" in POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL
    )
    assert "last_duplicate_at TIMESTAMPTZ" in POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL
    assert "idx_settlement_records_last_duplicate_at" in POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL
    assert (
        "RENAME COLUMN project_id TO seller_account_id" in POSTGRES_SETTLEMENT_SELLER_ACCOUNTS_SQL
    )
    assert (
        "payment_profile_id TEXT NOT NULL DEFAULT 'default'"
        in POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL
    )
    assert (
        "idx_settlement_records_provider_fingerprint_lookup"
        in POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL
    )
    assert (
        "idx_settlement_records_provider_fingerprint_lookup"
        in POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL
    )
    assert "DROP INDEX IF EXISTS uniq_settlement_records_provider_fingerprint" in (
        POSTGRES_SETTLEMENT_SELLER_ACCOUNTS_SQL + POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL
    )
    assert "seller_account_id" in POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL
    assert "CREATE UNIQUE INDEX" not in POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL
    assert "hosted_project_api_keys" not in POSTGRES_SELLER_PROJECT_COMPATIBILITY_SQL
    assert "INSERT INTO hosted_tenants" in POSTGRES_SELLER_PROJECT_COMPATIBILITY_SQL
    assert "migrated_%" in POSTGRES_SELLER_REVOKE_PROJECT_API_KEYS_SQL
    assert "target_type = 'seller'" in POSTGRES_CONTROL_PLANE_SELLER_AUDIT_TARGETS_SQL
    assert "target_type = 'project'" in POSTGRES_CONTROL_PLANE_SELLER_AUDIT_TARGETS_SQL
    assert "control_plane_audit_events_target_type_check" in (
        POSTGRES_CONTROL_PLANE_TARGET_TYPE_CONSTRAINT_SQL
    )
    assert "pg_advisory_xact_lock" in PROVIDER_FINGERPRINT_LOCK_SQL
    assert "ON CONFLICT DO NOTHING" in CLAIM_RECORD_SQL


def test_hosted_postgres_migration_checksums_are_pinned():
    migrations = (
        *POSTGRES_SETTLEMENT_MIGRATIONS,
        *POSTGRES_CONTROL_PLANE_MIGRATIONS,
        *POSTGRES_SELLER_ACCOUNTS_MIGRATIONS,
    )

    assert [
        (
            migration.scope,
            migration.version,
            migration.description,
            migration.checksum,
            migration.accepted_checksums,
        )
        for migration in migrations
    ] == [
        (
            "settlement",
            "202605170001",
            "create hosted settlement records and attempts",
            "7b4ec8b04fba2819b336d30e24a412a9fbdaffd011aa79bb837c8ea03cc4675c",
            (
                "aa4b4719822ff7e9b96799a14e9a6c1d2e28e48ec419ca62b275497efd8b1375",
                "aa4b47eb4fd06d49263843f6b786a76bc95abf7faf68f1c97bce7b33b277f73f",
            ),
        ),
        (
            "settlement",
            "202605200001",
            "add hosted settlement duplicate metrics",
            "632834d6e72a19c15bebf3619f1f8102fd2cbcf4ace5c9b510a61bc9c757eada",
            (),
        ),
        (
            "settlement",
            "2026052000011",
            "rename hosted settlement records from projects to seller accounts",
            "3c5af20de415046a78dc86bbb851880e43af6767f8b4dfac1335a44148f9e16f",
            ("45c4bb9f5b633879433164721ab09c08cdc1e0b8f1c0675f7fc3ee92b5ba8318",),
        ),
        (
            "settlement",
            "202605200002",
            "scope hosted settlement records to payment profiles",
            "1cb6961c583c97e1faa4184575c00b96d8329edc3aae38cf98479fa0f0cc8e32",
            (
                "f8f85195efb18a12b045c9e8b0fa94280cdec5274e28e91d1078ef56dce092a9",
                "931eac5c3821a3df41cbe9e9eae107d3b87fd5fc7a464179ac622c1779b0ebe7",
                "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
                "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
            ),
        ),
        (
            "settlement",
            "202605200003",
            "re-assert hosted settlement payment profile schema",
            "1cb6961c583c97e1faa4184575c00b96d8329edc3aae38cf98479fa0f0cc8e32",
            (
                "f8f85195efb18a12b045c9e8b0fa94280cdec5274e28e91d1078ef56dce092a9",
                "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
                "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
            ),
        ),
        (
            "settlement",
            "202605200004",
            "re-assert hosted settlement final constraints and lookup index",
            "1cb6961c583c97e1faa4184575c00b96d8329edc3aae38cf98479fa0f0cc8e32",
            (
                "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
                "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
            ),
        ),
        (
            "settlement",
            "202605200005",
            "drop stale global provider fingerprint index",
            "1829d45c24714e0d6ee5375fcc77b8a00dff78fc658dfe8649343a43ae40cf96",
            ("23a62acc1523d854ab1f6725aa725d2d13a5be1be61a8c9bb6cc8fad4f7bf77a",),
        ),
        (
            "settlement",
            "202605200006",
            "scope provider fingerprint replay to sellers",
            "1829d45c24714e0d6ee5375fcc77b8a00dff78fc658dfe8649343a43ae40cf96",
            (),
        ),
        (
            "settlement",
            "202605280002",
            "index seller scoped settlement health queries",
            "222300dab4b3bf7cc9c453661bcd53140fe74b3ff8a7d520c3888ff8e3233468",
            (),
        ),
        (
            "control_plane",
            "202605170001",
            "create hosted control-plane pause and audit tables",
            "96f88c5184f45e3040387a4915bb097756ab04a0292e72ea20ca08c26a193631",
            (),
        ),
        (
            "control_plane",
            "202605200001",
            "rename hosted control-plane project audit targets to sellers",
            "6a54648e1e16ab0f54cec1dfb17cf367bd70e48037819081e4c6f6e2fbc02777",
            (),
        ),
        (
            "control_plane",
            "202605200002",
            "constrain hosted control-plane audit target types",
            "a39fa86897820b97c2496b0f9ef328ce400b23df61208bac910797bd1bdb819f",
            (),
        ),
        (
            "control_plane",
            "202605280001",
            "allow hosted control-plane settlement audit targets",
            "8e65b2d347f3959eed09adcf4173849e4cea0285a336164012d66078c06cb468",
            (),
        ),
        (
            "seller_accounts",
            "202605170001",
            "create hosted tenants, seller accounts, and seller API keys",
            "3df818d52070461324365d384e71811de7f4f6af887833896d876660312bd7c5",
            ("66a51f11deff8111ac18a037f21374f230e35b3f99dfc623e5428a59cecfc285",),
        ),
        (
            "seller_accounts",
            "202605200001",
            "create hosted seller payment profiles",
            "0aacc35d82a66a33c7490c01889f6f1583a0e562a960be1c181fb1364bc225ef",
            ("33b2531c827a71a0ed4ebd02edcdb7ba7f20a58e6d3c9c75389d2f81d4342e36",),
        ),
        (
            "seller_accounts",
            "202605200002",
            "copy project-scoped sellers into seller accounts",
            "9bc221ef62f99ec899a54ee11a258c04bdba3f30ec59a40e2cb6538ce937c30a",
            (
                "0952f75c69778db7f0922d4a5d77f2e3e9e05696dc7bb98dda5ab6321f83c4e7",
                "5610611582a076fe05e2850de3abc248c1850e3356d06f957fc6ea8539aeca22",
            ),
        ),
        (
            "seller_accounts",
            "202605200003",
            "remove project-imported hosted seller API keys",
            "220421485abf83afde24afde7bae217c15cb6b04e603ffd3540af5274b3e9dda",
            (),
        ),
    ]


def test_postgres_control_plane_schema_has_required_tables_and_indexes():
    assert (
        "CREATE TABLE IF NOT EXISTS control_plane_pause_targets"
        in POSTGRES_CONTROL_PLANE_SCHEMA_SQL
    )
    assert "PRIMARY KEY(target_type, target)" in POSTGRES_CONTROL_PLANE_SCHEMA_SQL
    assert (
        "CREATE TABLE IF NOT EXISTS control_plane_audit_events" in POSTGRES_CONTROL_PLANE_SCHEMA_SQL
    )
    assert "correlation_id TEXT NOT NULL" in POSTGRES_CONTROL_PLANE_SCHEMA_SQL
    assert "idx_control_plane_pause_targets_paused" in POSTGRES_CONTROL_PLANE_SCHEMA_SQL
    assert "idx_control_plane_audit_events_created" in POSTGRES_CONTROL_PLANE_SCHEMA_SQL


def test_postgres_health_check_verifies_schema_objects():
    connection = _Connection(
        rows=[
            {
                "settlement_records_table": True,
                "settlement_attempts_table": True,
                "seller_profile_fingerprint_unique": True,
                "no_stale_seller_fingerprint_unique": True,
                "status_updated_index": True,
                "provider_network_status_index": True,
                "reconciliation_lease_index": True,
                "attempts_record_started_index": True,
                "duplicate_claims_column": True,
                "last_duplicate_at_column": True,
                "last_duplicate_at_index": True,
                "provider_fingerprint_lookup_index": True,
                "seller_status_updated_index": True,
            },
            _applied_migration_rows(POSTGRES_SETTLEMENT_MIGRATIONS),
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    assert store.health_check() is True
    assert POSTGRES_HEALTH_SQL in connection.calls[0][0]
    assert "current_schema()" in connection.calls[0][0]
    assert "seller_profile_fingerprint_unique" in connection.calls[0][0]
    assert SELECT_APPLIED_MIGRATIONS_SQL in connection.calls[1][0]


def test_postgres_health_check_fails_when_schema_objects_are_missing():
    connection = _Connection(
        rows=[
            {
                "settlement_records_table": True,
                "settlement_attempts_table": False,
                "seller_profile_fingerprint_unique": True,
                "no_stale_seller_fingerprint_unique": True,
                "status_updated_index": True,
                "provider_network_status_index": True,
                "reconciliation_lease_index": True,
                "attempts_record_started_index": True,
                "duplicate_claims_column": True,
                "last_duplicate_at_column": True,
                "last_duplicate_at_index": True,
                "provider_fingerprint_lookup_index": True,
            }
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    assert store.health_check() is False


def test_postgres_health_check_fails_when_migration_ledger_is_not_current():
    connection = _Connection(
        rows=[
            {
                "settlement_records_table": True,
                "settlement_attempts_table": True,
                "seller_profile_fingerprint_unique": True,
                "no_stale_seller_fingerprint_unique": True,
                "status_updated_index": True,
                "provider_network_status_index": True,
                "reconciliation_lease_index": True,
                "attempts_record_started_index": True,
                "duplicate_claims_column": True,
                "last_duplicate_at_column": True,
                "last_duplicate_at_index": True,
                "provider_fingerprint_lookup_index": True,
            },
            [],
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    assert store.health_check() is False


def test_postgres_health_check_fails_when_migration_ledger_has_future_version():
    future_rows = [
        *_applied_migration_rows(POSTGRES_SETTLEMENT_MIGRATIONS),
        {"version": "299901010001", "checksum": "future-checksum"},
    ]
    connection = _Connection(
        rows=[
            {
                "settlement_records_table": True,
                "settlement_attempts_table": True,
                "seller_profile_fingerprint_unique": True,
                "no_stale_seller_fingerprint_unique": True,
                "status_updated_index": True,
                "provider_network_status_index": True,
                "reconciliation_lease_index": True,
                "attempts_record_started_index": True,
                "duplicate_claims_column": True,
                "last_duplicate_at_column": True,
                "last_duplicate_at_index": True,
                "provider_fingerprint_lookup_index": True,
            },
            future_rows,
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    assert store.health_check() is False


def test_postgres_settlement_initialize_rejects_schema_drift_before_recording_migration():
    connection = _Connection(
        rows=[
            [],
            {
                "settlement_records_table": True,
                "settlement_attempts_table": True,
                "seller_profile_fingerprint_unique": False,
                "no_stale_seller_fingerprint_unique": True,
                "status_updated_index": True,
                "provider_network_status_index": True,
                "reconciliation_lease_index": True,
                "attempts_record_started_index": True,
                "duplicate_claims_column": True,
                "last_duplicate_at_column": True,
                "last_duplicate_at_index": True,
                "provider_fingerprint_lookup_index": True,
            },
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    with pytest.raises(RuntimeError, match="schema validation failed"):
        store.initialize()

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL not in rendered_calls
    assert connection.rollbacks == 1


def test_postgres_control_plane_health_check_verifies_schema_objects():
    connection = _Connection(
        rows=[
            {
                "control_plane_pause_targets_table": True,
                "control_plane_audit_events_table": True,
                "pause_targets_primary_key": True,
                "pause_targets_paused_index": True,
                "audit_events_created_index": True,
            },
            _applied_migration_rows(POSTGRES_CONTROL_PLANE_MIGRATIONS),
        ]
    )
    state = PostgresControlPlaneState(connection=connection)

    assert state.health_check() is True
    assert POSTGRES_CONTROL_PLANE_HEALTH_SQL in connection.calls[0][0]
    assert "ARRAY['target_type', 'target']" in connection.calls[0][0]
    assert SELECT_APPLIED_MIGRATIONS_SQL in connection.calls[1][0]


def test_postgres_seller_account_resolver_initializes_and_checks_health():
    connection = _Connection(
        rows=[
            [],
            _project_schema_health_row(),
            _project_schema_health_row(),
            _applied_migration_rows(POSTGRES_SELLER_ACCOUNTS_MIGRATIONS),
        ]
    )
    resolver = PostgresSellerAccountResolver(connection=connection)

    resolver.initialize()
    assert resolver.health_check() is True

    rendered_calls = "\n".join(sql for sql, _ in connection.calls)
    assert HOSTED_SCHEMA_MIGRATIONS_SQL in rendered_calls
    assert POSTGRES_SELLER_ACCOUNTS_SCHEMA_SQL in rendered_calls
    assert INSERT_APPLIED_MIGRATION_SQL in rendered_calls
    assert any(POSTGRES_SELLER_ACCOUNTS_HEALTH_SQL in sql for sql, _ in connection.calls)
    assert "idx_hosted_seller_accounts_tenant_status" in rendered_calls
    assert "ARRAY['key_hash']" in rendered_calls
    assert "ARRAY['key_id']" in rendered_calls
    assert "ARRAY['tenant_id']" in rendered_calls
    assert "ARRAY['seller_account_id', 'id']" in rendered_calls
    assert "ARRAY['seller_account_id', 'payment_profile_id']" in rendered_calls


def test_postgres_seller_account_resolver_health_fails_without_composite_profile_fk():
    connection = _Connection(
        rows=[_project_schema_health_row(api_keys_payment_profile_foreign_key=False)]
    )
    resolver = PostgresSellerAccountResolver(connection=connection)

    assert resolver.health_check() is False


def test_postgres_seller_account_resolver_resolves_api_key_by_hash():
    api_key = "omck_secret_key"
    connection = _Connection(rows=[_project_row()])
    resolver = PostgresSellerAccountResolver(connection=connection)

    project = resolver.resolve(authorization=f"Bearer {api_key}", require_auth=True)
    sql, params = connection.calls[-1]

    assert project.seller_account_id == "seller-a"
    assert "hosted_seller_api_keys.key_hash = %s" in sql
    assert params == (seller_key_hash(api_key),)
    assert api_key not in str(connection.calls)


def test_postgres_seller_account_resolver_resolves_api_key_to_payment_profile():
    api_key = "omck_secret_key"
    connection = _Connection(
        rows=[
            _project_row(
                profile_id="profile-b",
                profile_name="Profile B",
                allowed_pay_to_json=["0xcccccccccccccccccccccccccccccccccccccccc"],
                rate_limits_json={"settlePerMinute": 3},
            )
        ]
    )
    resolver = PostgresSellerAccountResolver(connection=connection)

    access = resolver.resolve_access(authorization=f"Bearer {api_key}", require_auth=True)

    assert access.seller_account.seller_account_id == "seller-a"
    assert access.payment_profile.payment_profile_id == "profile-b"
    assert access.payment_profile.allowed_pay_to == ("0xcccccccccccccccccccccccccccccccccccccccc",)
    assert access.payment_profile.rate_limits.settle_per_minute == 3


def test_postgres_seller_account_resolver_default_project_requires_active_tenant_and_project():
    connection = _Connection()
    resolver = PostgresSellerAccountResolver(
        connection=connection, default_seller_account_id="seller-a"
    )

    with pytest.raises(PermissionError, match="Seller API-key authentication is required"):
        resolver.resolve(require_auth=False)
    sql, params = connection.calls[-1]

    assert "hosted_seller_accounts.status = 'active'" in sql
    assert "hosted_tenants.status = 'active'" in sql
    assert params == ("seller-a",)


def test_postgres_seller_account_resolver_sql_rejects_revoked_or_disabled_api_key():
    api_key = "omck_secret_key"
    connection = _Connection()
    resolver = PostgresSellerAccountResolver(connection=connection)

    with pytest.raises(PermissionError, match="Invalid facilitator API key"):
        resolver.resolve(authorization=f"Bearer {api_key}", require_auth=True)
    sql, params = connection.calls[-1]

    assert "hosted_seller_api_keys.status = 'active'" in sql
    assert "hosted_seller_api_keys.revoked_at IS NULL" in sql
    assert "hosted_seller_accounts.status = 'active'" in sql
    assert "hosted_tenants.status = 'active'" in sql
    assert params == (seller_key_hash(api_key),)


def test_postgres_seller_account_resolver_rejects_missing_auth_when_required():
    resolver = PostgresSellerAccountResolver(connection=_Connection())

    with pytest.raises(PermissionError, match="Seller API-key authentication is required"):
        resolver.resolve(require_auth=True)


def test_postgres_seller_account_resolver_issues_keys_without_persisting_raw_key():
    connection = _Connection(rows=[{"id": "default"}])
    resolver = PostgresSellerAccountResolver(connection=connection)

    api_key = resolver.issue_api_key("seller-a")

    assert api_key.key.startswith("omck_")
    rendered_calls = str(connection.calls)
    assert api_key.key not in rendered_calls
    assert api_key.key_hash in rendered_calls
    assert "hosted_seller_accounts.status = 'active'" in rendered_calls
    assert "hosted_tenants.status = 'active'" in rendered_calls
    assert connection.transactions == 1


def test_postgres_seller_account_resolver_issues_key_for_requested_payment_profile():
    connection = _Connection(rows=[{"id": "profile-b"}])
    resolver = PostgresSellerAccountResolver(connection=connection)

    api_key = resolver.issue_api_key("seller-a", "profile-b")

    insert_sql, insert_params = connection.calls[-1]
    assert api_key.payment_profile_id == "profile-b"
    assert "payment_profile_id" in insert_sql
    assert insert_params[3] == "profile-b"


def test_postgres_bootstrap_api_key_rejects_cross_profile_reassignment():
    connection = _Connection(
        rows=[{"seller_account_id": "seller-b", "payment_profile_id": "default"}]
    )
    resolver = PostgresSellerAccountResolver(connection=connection)

    with pytest.raises(ValueError, match="Duplicate seller API key configured"):
        resolver._upsert_bootstrap_api_key(
            api_key="omck_shared",
            seller_account_id="seller-a",
            payment_profile_id="default",
            now=utcnow(),
        )


def test_postgres_control_plane_set_pause_is_transactional_and_audited():
    now = utcnow()
    connection = _Connection(
        rows=[
            {"paused": False},
            {
                "id": 17,
                "action": "pause_set",
                "target_type": "provider",
                "target": "exact_evm",
                "before_paused": False,
                "after_paused": True,
                "reason": "operator_supplied",
                "actor": "ops@example.com",
                "correlation_id": "cp-123",
                "created_at": now,
            },
        ]
    )
    state = PostgresControlPlaneState(connection=connection, hosted_safe=True)

    event = state.set_pause(
        target_type=ControlPlaneTargetType.PROVIDER,
        target="exact_evm",
        paused=True,
        reason="rotate leaked sk_live_ABC123",
        actor="ops@example.com",
        correlation_id="cp-123",
    )

    assert state.durable is True
    assert state.hosted_safe is True
    assert state.writes_enabled is True
    assert connection.transactions == 1
    assert event.event_id == 17
    assert event.before is False
    assert event.after is True
    assert event.reason == "operator_supplied"
    assert event.actor == "ops@example.com"
    assert event.correlation_id == "cp-123"
    assert any("control_plane_pause_targets" in sql for sql, _params in connection.calls)
    assert any("control_plane_audit_events" in sql for sql, _params in connection.calls)
    assert any("FOR UPDATE" in sql for sql, _params in connection.calls)


def test_postgres_control_plane_pause_state_and_audit_tail_are_sanitized():
    now = utcnow()
    connection = _Connection(
        rows=[
            [
                {"target_type": "global", "target": "global"},
                {"target_type": "provider", "target": "exact_evm"},
                {"target_type": "network", "target": "eip155:5042002"},
                {"target_type": "network", "target": "postgres://secret@db.internal"},
            ],
            [
                {
                    "id": 19,
                    "action": "pause_set",
                    "target_type": "project",
                    "target": "seller_legacy",
                    "before_paused": False,
                    "after_paused": True,
                    "reason": "sk_live_ABC123",
                    "actor": "ops@example.com",
                    "correlation_id": "cp-123",
                    "created_at": now,
                },
                {
                    "id": 18,
                    "action": "pause_set",
                    "target_type": "invalid",
                    "target": "bad-target",
                    "before_paused": False,
                    "after_paused": True,
                    "reason": "operator maintenance",
                    "actor": "ops@example.com",
                    "correlation_id": "cp-invalid",
                    "created_at": now,
                },
            ],
        ]
    )
    state = PostgresControlPlaneState(connection=connection, hosted_safe=True)

    pause_state = state.pause_state()
    audit_tail = state.audit_tail()

    assert pause_state.global_paused is True
    assert pause_state.providers == {"exact_evm"}
    assert pause_state.networks == {"eip155:5042002"}
    assert audit_tail[0].target_type == ControlPlaneTargetType.SELLER
    assert audit_tail[0].target == "seller_legacy"
    assert len(audit_tail) == 1
    assert audit_tail[0].reason == "operator_supplied"
    assert audit_tail[0].correlation_id == "cp-123"


def test_postgres_control_plane_records_settlement_reconciliation_audit_events():
    now = utcnow()
    connection = _Connection(
        rows=[
            {
                "id": 29,
                "action": "reconciliation_claim",
                "target_type": "settlement",
                "target": "42",
                "before_paused": False,
                "after_paused": True,
                "reason": "operator_supplied",
                "actor": "ops@example.com",
                "correlation_id": "rec-123",
                "created_at": now,
            }
        ]
    )
    state = PostgresControlPlaneState(connection=connection, hosted_safe=True)

    event = state.record_reconciliation_event(
        action="reconciliation_claim",
        record_id=42,
        before=False,
        after=True,
        reason="contains sk_live_ABC123",
        actor="ops@example.com",
        correlation_id="rec-123",
    )
    sql, params = connection.calls[-1]

    assert event.target_type == ControlPlaneTargetType.SETTLEMENT
    assert event.target == "42"
    assert event.reason == "operator_supplied"
    assert "control_plane_audit_events" in sql
    assert params[1] == "settlement"
    assert params[5] == "operator_supplied"


def test_postgres_control_plane_migrations_allow_settlement_audit_targets():
    assert "settlement" in POSTGRES_CONTROL_PLANE_SETTLEMENT_AUDIT_TARGET_SQL
    assert any(
        migration.version == "202605280001"
        and migration.sql == POSTGRES_CONTROL_PLANE_SETTLEMENT_AUDIT_TARGET_SQL
        for migration in POSTGRES_CONTROL_PLANE_MIGRATIONS
    )


def test_postgres_claim_and_attempt_are_one_transaction():
    connection = _Connection(rows=[None, _row(inserted=True), {"id": 11}])
    store = PostgresSettlementStore(connection=connection)

    record, claim, attempt_id = store.claim_and_start_settle_attempt(
        seller_account_id="default",
        fingerprint="fp",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_pg",
        raw_requirements={"resource": "https://seller.example.com/private"},
    )

    assert claim == SettlementClaim.CLAIMED
    assert attempt_id == 11
    assert record.record_id == 7
    assert connection.transactions == 1
    assert PROVIDER_FINGERPRINT_LOCK_SQL in connection.calls[0][0]
    assert connection.calls[0][1] == ("default:exact_evm:exact:eip155:5042002:fp",)
    assert CLAIM_RECORD_SQL in connection.calls[2][0]
    assert INSERT_ATTEMPT_SQL in connection.calls[3][0]
    assert "private" not in connection.calls[2][1][8]


def test_postgres_duplicate_claim_does_not_create_attempt():
    connection = _Connection(rows=[_row(inserted=False, status=SettlementStatus.UNKNOWN)])
    store = PostgresSettlementStore(connection=connection)

    record, claim, attempt_id = store.claim_and_start_settle_attempt(
        seller_account_id="default",
        fingerprint="fp",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_pg",
        raw_requirements={"resource": "https://seller.example.com/private"},
    )

    assert record.status == SettlementStatus.UNKNOWN
    assert claim == SettlementClaim.DUPLICATE_UNKNOWN
    assert attempt_id is None
    assert len(connection.calls) == 3
    assert PROVIDER_FINGERPRINT_LOCK_SQL in connection.calls[0][0]
    assert "UPDATE settlement_records" in connection.calls[2][0]
    assert "duplicate_claims = duplicate_claims + 1" in connection.calls[2][0]


def test_postgres_provider_replay_is_scoped_to_seller_without_requiring_same_profile():
    connection = _Connection(
        rows=[
            _row(
                inserted=False,
                status=SettlementStatus.SETTLED,
                payment_profile_id="profile-a",
            ),
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    record, claim, attempt_id = store.claim_and_start_settle_attempt(
        seller_account_id="default",
        payment_profile_id="profile-b",
        fingerprint="fp",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_pg",
        raw_requirements={"resource": "https://seller.example.com/private"},
    )

    assert record.status == SettlementStatus.SETTLED
    assert record.payment_profile_id == "profile-a"
    assert claim == SettlementClaim.DUPLICATE_FINAL
    assert attempt_id is None
    assert PROVIDER_FINGERPRINT_LOCK_SQL in connection.calls[0][0]
    assert "WHERE seller_account_id = %s" in connection.calls[1][0]
    assert "payment_profile_id = %s" not in connection.calls[1][0]
    assert all("ON CONFLICT DO NOTHING" not in sql for sql, _ in connection.calls)
    assert len(connection.calls) == 3


def test_postgres_same_profile_provider_replay_returns_duplicate_record():
    connection = _Connection(
        rows=[
            _row(
                inserted=False,
                status=SettlementStatus.SETTLED,
                payment_profile_id="profile-b",
            ),
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    record, claim, attempt_id = store.claim_and_start_settle_attempt(
        seller_account_id="default",
        payment_profile_id="profile-b",
        fingerprint="fp",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_pg",
        raw_requirements={"resource": "https://seller.example.com/private"},
    )

    assert record.status == SettlementStatus.SETTLED
    assert record.payment_profile_id == "profile-b"
    assert claim == SettlementClaim.DUPLICATE_FINAL
    assert attempt_id is None
    assert PROVIDER_FINGERPRINT_LOCK_SQL in connection.calls[0][0]
    assert "WHERE seller_account_id = %s" in connection.calls[1][0]
    assert all("ON CONFLICT DO NOTHING" not in sql for sql, _ in connection.calls)
    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_engine_uses_postgres_atomic_claim_attempt_path():
    connection = _Connection(rows=[None, _row(inserted=True), {"id": 11}])
    store = PostgresSettlementStore(connection=connection)
    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]), store=store)
    context = RequestContext(
        trace_id="tr_pg",
        seller_account=StaticSellerAccountResolver().resolve(require_auth=False),
    )
    envelope = FacilitatorEnvelope.model_validate(
        {
            "x402Version": 2,
            "paymentPayload": {
                "x402Version": 2,
                "payload": {
                    "signature": "0xsig",
                    "authorization": {
                        "from": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "to": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        "value": "250000",
                        "validAfter": "0",
                        "validBefore": "9999999999",
                        "nonce": "0x" + "11" * 32,
                    },
                },
                "accepted": {
                    "scheme": "exact",
                    "network": "eip155:5042002",
                    "asset": "0x3600000000000000000000000000000000000000",
                    "amount": "250000",
                    "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "extra": {"name": "USDC"},
                },
            },
            "paymentRequirements": {
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "amount": "250000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "resource": "https://seller.example.com/private",
                "extra": {"name": "USDC"},
            },
        }
    )

    outcome = await engine.settle(envelope, context)

    assert outcome.success is True
    assert connection.transactions == 2
    assert INSERT_ATTEMPT_SQL in connection.calls[3][0]
    assert all("INSERT INTO settlement_attempts" not in sql for sql, _ in connection.calls[4:])


def test_postgres_store_is_accepted_in_non_local_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_oidc_openfga_env(monkeypatch)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )
    store = PostgresSettlementStore(connection=_Connection(), hosted_safe=True)
    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]), store=store)

    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver(),
        rate_limiter=_HostedSafeRateLimiter(),
        telemetry_config=_hosted_otel_config(),
    )

    assert TestClient(app).get("/supported").status_code == 200


def test_postgres_store_factory_is_accepted_in_non_local_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_oidc_openfga_env(monkeypatch)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.telemetry._configure_otel_providers",
        lambda _config: None,
    )
    factory_calls = 0

    def store_factory():
        nonlocal factory_calls
        factory_calls += 1
        return PostgresSettlementStore(connection=_Connection(), hosted_safe=True)

    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]))

    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=_HostedSafeProjectResolver(),
        store_factory=store_factory,
        rate_limiter=_HostedSafeRateLimiter(),
        telemetry_config=_hosted_otel_config(),
    )

    assert factory_calls == 1
    assert TestClient(app).get("/supported").status_code == 200


def test_postgres_store_pool_reuses_connections_and_closes_idle(monkeypatch):
    connections: list[_ClosableConnection] = []

    def connect(_dsn):
        connection = _ClosableConnection(
            rows=[
                {
                    "settlement_table": True,
                    "attempt_table": True,
                    "unique_constraint": True,
                    "attempt_foreign_key": True,
                    "reconciliation_columns": True,
                    "reconciliation_lease_index": True,
                },
                {"count": 1},
            ]
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr("hosted_facilitator.hosted.postgres._connect", connect)
    pool = PostgresSettlementStorePool("postgres://example", max_size=1)

    first = pool.store()
    first_connection = first._conn._connection
    first.close()
    second = pool.store()
    second_connection = second._conn._connection
    second.close()
    pool.close()

    assert len(connections) == 1
    assert first_connection is second_connection
    assert connections[0].closed == 1


def test_unsafe_postgres_store_factory_is_rejected_in_non_local_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]))

    with pytest.raises(RuntimeError, match="approved hosted settlement store factory"):
        create_hosted_facilitator_app(
            engine=engine,
            resolver=StaticSellerAccountResolver(),
            store_factory=lambda: PostgresSettlementStore(connection=_Connection()),
        )


def test_injected_postgres_connection_is_not_hosted_safe_by_default(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    store = PostgresSettlementStore(connection=_Connection())
    engine = HostedFacilitatorEngine(router=ProviderRouter([_Provider()]), store=store)

    try:
        create_hosted_facilitator_app(engine=engine, resolver=StaticSellerAccountResolver())
    except RuntimeError as exc:
        assert "approved hosted settlement store" in str(exc)
    else:
        raise AssertionError("expected injected Postgres connection to fail hosted-safe guard")


def test_postgres_store_from_env_requires_dsn(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    try:
        postgres_store_from_env()
    except RuntimeError as exc:
        assert "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN" in str(exc)
    else:
        raise AssertionError("expected postgres_store_from_env to require a DSN")


def test_postgres_control_plane_state_from_env_uses_dedicated_dsn(monkeypatch):
    seen = {}

    def connect(dsn):
        seen["dsn"] = dsn
        return _Connection()

    monkeypatch.setattr("hosted_facilitator.hosted.postgres._connect", connect)
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")

    state = postgres_control_plane_state_from_env()

    assert state.hosted_safe is True
    assert seen["dsn"] == "postgresql://control"


def test_postgres_control_plane_state_from_env_requires_dsn(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres control-plane state requires"):
        postgres_control_plane_state_from_env()


def test_postgres_seller_account_resolver_from_env_uses_dedicated_dsn(monkeypatch):
    seen = {}

    def connect(dsn):
        seen["dsn"] = dsn
        return _Connection()

    monkeypatch.setattr("hosted_facilitator.hosted.postgres._connect", connect)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")

    resolver = postgres_seller_account_resolver_from_env()

    assert resolver.hosted_safe is True
    assert seen["dsn"] == "postgresql://seller-accounts"


def test_postgres_seller_account_resolver_from_env_requires_dsn(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="Postgres seller account resolver requires"):
        postgres_seller_account_resolver_from_env()


def test_postgres_store_commits_terminal_updates():
    connection = _Connection()
    store = PostgresSettlementStore(connection=connection)
    record = _row(inserted=True)
    settlement = store.get("default", "fp")
    assert settlement is None
    connection.rows.append(record)
    settlement = store.get("default", "fp")
    assert settlement is not None

    store.mark_settled(settlement, transaction="0xpg", payer="0xpayer")
    store.finish_settle_attempt(1, status="settled", transaction="0xpg")

    assert connection.commits == 2


def test_postgres_terminal_updates_are_transition_guarded():
    connection = _Connection(rows=[_row(inserted=True)])
    store = PostgresSettlementStore(connection=connection)
    settlement = store.get("default", "fp")
    assert settlement is not None

    store.mark_settled(settlement, transaction="0xpg", payer="0xpayer")
    sql, params = connection.calls[-1]

    assert "WHERE id = %s AND status = ANY(%s)" in sql
    assert params[-1] == ["settle_in_progress", "submitted"]


def test_postgres_terminal_update_raises_when_transition_guard_misses():
    connection = _Connection(rows=[_row(inserted=True)], rowcounts=[1, 0])
    store = PostgresSettlementStore(connection=connection)
    settlement = store.get("default", "fp")
    assert settlement is not None

    with pytest.raises(RuntimeError, match="not in an expected status"):
        store.mark_settle_failed(settlement, error_reason="stale")

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert settlement.status == SettlementStatus.SETTLE_IN_PROGRESS


def test_postgres_lists_reconciliation_candidates_with_lease_guard():
    now = utcnow()
    connection = _Connection(rows=[[_row(inserted=True, status=SettlementStatus.UNKNOWN)]])
    store = PostgresSettlementStore(connection=connection)

    records = store.list_reconciliation_candidates(
        statuses=(SettlementStatus.UNKNOWN, SettlementStatus.SUBMITTED),
        stale_before=now,
        limit=25,
    )
    sql, params = connection.calls[-1]

    assert records[0].status == SettlementStatus.UNKNOWN
    assert LIST_RECONCILIATION_CANDIDATES_SQL in sql
    assert params[0] == ["unknown", "submitted"]
    assert params[1] == now
    assert params[3] == 25


def test_postgres_claims_reconciliation_record_with_owner_lease():
    now = utcnow()
    lease_until = now.replace(year=now.year + 1)
    connection = _Connection(rows=[_row(inserted=True, status=SettlementStatus.UNKNOWN)])
    store = PostgresSettlementStore(connection=connection)

    record = store.claim_reconciliation_record(
        record_id=7,
        owner="reconciler-a",
        lease_until=lease_until,
        eligible_statuses=(SettlementStatus.UNKNOWN, SettlementStatus.SUBMITTED),
        stale_before=now,
    )
    sql, params = connection.calls[-1]

    assert record is not None
    assert record.record_id == 7
    assert CLAIM_RECONCILIATION_RECORD_SQL in sql
    assert params[:4] == (
        "reconciler-a",
        lease_until,
        7,
        ["unknown", "submitted"],
    )
    assert connection.commits == 1


def test_postgres_releases_reconciliation_record_with_owner_guard():
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner=None,
                reconciliation_lease_until=None,
            )
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    record = store.release_reconciliation_record(record_id=7, owner="ops@example.com")
    sql, params = connection.calls[-1]

    assert record is not None
    assert record.record_id == 7
    assert "reconciliation_owner = NULL" in sql
    assert "AND reconciliation_owner = %s" in sql
    assert params[1:] == (7, "ops@example.com")
    assert connection.commits == 1


def test_postgres_applies_reconciliation_action_and_audit_in_one_transaction():
    now = utcnow()
    owner = _safe_operator_lease_owner("ops@example.com", issuer="https://idp.example.test")
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner=None,
                reconciliation_lease_until=None,
            ),
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner=owner,
                reconciliation_lease_until=now + timedelta(minutes=5),
            ),
            {
                "id": 31,
                "action": "reconciliation_claim",
                "target_type": "settlement",
                "target": "7",
                "before_paused": False,
                "after_paused": True,
                "reason": "operator_supplied",
                "actor": "ops@example.com",
                "correlation_id": "rec-atomic",
                "created_at": now,
            },
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    record, event = store.apply_reconciliation_control_action(
        action="reconciliation_claim",
        record_id=7,
        owner=owner,
        lease_until=now + timedelta(minutes=5),
        active_stale_before=now - timedelta(minutes=5),
        reason="operator triage",
        actor="ops@example.com",
        correlation_id="rec-atomic",
    )

    assert connection.transactions == 1
    assert record.reconciliation_owner == owner
    assert event.action == "reconciliation_claim"
    assert event.target_type == ControlPlaneTargetType.SETTLEMENT
    assert any("FOR UPDATE" in sql for sql, _params in connection.calls)
    assert any("control_plane_audit_events" in sql for sql, _params in connection.calls)


def test_postgres_reconciliation_action_rolls_back_when_audit_insert_fails():
    now = utcnow()
    owner = _safe_operator_lease_owner("ops@example.com", issuer="https://idp.example.test")
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner=None,
                reconciliation_lease_until=None,
            ),
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner=owner,
                reconciliation_lease_until=now + timedelta(minutes=5),
            ),
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    with pytest.raises(
        RuntimeError, match="Postgres control-plane reconciliation audit event was not created"
    ):
        store.apply_reconciliation_control_action(
            action="reconciliation_claim",
            record_id=7,
            owner=owner,
            lease_until=now + timedelta(minutes=5),
            active_stale_before=now - timedelta(minutes=5),
            reason="operator triage",
            actor="ops@example.com",
            correlation_id="rec-atomic",
        )

    assert connection.transactions == 1
    assert connection.rollbacks == 1


def test_postgres_reconciliation_action_rejects_fresh_active_claims():
    now = utcnow()
    owner = _safe_operator_lease_owner("ops@example.com", issuer="https://idp.example.test")
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.SUBMITTED,
                reconciliation_owner=None,
                reconciliation_lease_until=None,
            )
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    with pytest.raises(RuntimeError, match="Active settlement record is not stale enough to claim"):
        store.apply_reconciliation_control_action(
            action="reconciliation_claim",
            record_id=7,
            owner=owner,
            lease_until=now + timedelta(minutes=5),
            active_stale_before=now - timedelta(minutes=5),
            reason="operator triage",
            actor="ops@example.com",
            correlation_id="rec-active",
        )

    assert not any("control_plane_audit_events" in sql for sql, _params in connection.calls)


def test_postgres_lists_stale_started_attempts():
    now = utcnow()
    connection = _Connection(
        rows=[
            [
                {
                    "id": 13,
                    "settlement_record_id": 7,
                    "trace_id": "tr_stale",
                    "status": "started",
                    "started_at": now,
                    "finished_at": None,
                    "transaction_hash": None,
                    "error_reason": None,
                }
            ]
        ]
    )
    store = PostgresSettlementStore(connection=connection)

    attempts = store.list_stale_started_attempts(started_before=now, limit=10)
    sql, params = connection.calls[-1]

    assert attempts[0].attempt_id == 13
    assert attempts[0].settlement_record_id == 7
    assert attempts[0].status.value == "started"
    assert LIST_STALE_STARTED_ATTEMPTS_SQL in sql
    assert params == ("started", now, 10)


def test_postgres_reconciled_terminal_updates_accept_unknown_records():
    lease_until = utcnow()
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner="reconciler-a",
                reconciliation_lease_until=lease_until,
            )
        ]
    )
    store = PostgresSettlementStore(connection=connection)
    settlement = store.get("default", "fp")
    assert settlement is not None

    store.mark_reconciled_settled(
        settlement,
        transaction="0xreconciled",
        payer="0xpayer",
        owner="reconciler-a",
    )
    sql, params = next(
        call for call in connection.calls if "reconciliation_owner = NULL" in call[0]
    )
    attempt_sql, attempt_params = connection.calls[-1]

    assert "reconciliation_owner = NULL" in sql
    assert "AND reconciliation_owner = %s" in sql
    assert "AND reconciliation_lease_until = %s" in sql
    assert "AND reconciliation_lease_until > %s" in sql
    assert params[-4] == ["unknown", "submitted", "settle_in_progress"]
    assert params[-3] == "reconciler-a"
    assert params[-2] == lease_until
    assert "UPDATE settlement_attempts" in attempt_sql
    assert attempt_params[0] == "settled"
    assert attempt_params[-1] == "started"
    assert settlement.status == SettlementStatus.SETTLED
    assert settlement.transaction == "0xreconciled"


def test_postgres_reconciled_update_rolls_back_when_attempt_closure_fails():
    lease_until = utcnow()
    connection = _FailingAttemptClosureConnection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.SETTLE_IN_PROGRESS,
                reconciliation_owner="reconciler-a",
                reconciliation_lease_until=lease_until,
            )
        ]
    )
    store = PostgresSettlementStore(connection=connection)
    settlement = store.get("default", "fp")
    assert settlement is not None

    with pytest.raises(RuntimeError, match="forced attempt closure failure"):
        store.mark_reconciled_unknown(
            settlement,
            error_reason="stale_settle_in_progress",
            owner="reconciler-a",
        )

    assert connection.transactions == 1
    assert connection.rollbacks == 1
    assert settlement.status == SettlementStatus.SETTLE_IN_PROGRESS
    assert settlement.error_reason is None


def test_postgres_reconciled_terminal_update_raises_when_lease_guard_misses():
    connection = _Connection(
        rows=[
            _row(
                inserted=True,
                status=SettlementStatus.UNKNOWN,
                reconciliation_owner="reconciler-a",
                reconciliation_lease_until=utcnow(),
            )
        ],
        rowcounts=[1, 0],
    )
    store = PostgresSettlementStore(connection=connection)
    settlement = store.get("default", "fp")
    assert settlement is not None

    with pytest.raises(RuntimeError, match="not leased by reconciler-b"):
        store.mark_reconciled_failed(
            settlement,
            error_reason="chain_reverted",
            owner="reconciler-b",
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert settlement.status == SettlementStatus.UNKNOWN


def test_postgres_datetime_coercion_preserves_timezone():
    connection = _Connection(rows=[_row(inserted=True), {"id": 11}])
    store = PostgresSettlementStore(connection=connection)

    record, _, _ = store.claim_and_start_settle_attempt(
        seller_account_id="default",
        fingerprint="fp",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="tr_pg",
        raw_requirements={},
    )

    assert record.created_at.tzinfo is not None
    assert record.created_at.astimezone(timezone.utc).tzinfo is not None
