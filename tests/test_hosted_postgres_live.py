from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from hosted_facilitator.hosted.app import create_hosted_facilitator_app
from hosted_facilitator.hosted.auth import Principal
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import (
    ControlPlaneTargetType,
    _safe_operator_lease_owner,
)
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.fingerprint import payment_fingerprint
from hosted_facilitator.hosted.migrations import (
    HOSTED_SCHEMA_MIGRATIONS_SQL,
    INSERT_APPLIED_MIGRATION_SQL,
)
from hosted_facilitator.hosted.postgres import (
    INSERT_ATTEMPT_SQL,
    POSTGRES_CONTROL_PLANE_MIGRATIONS,
    POSTGRES_CONTROL_PLANE_SCHEMA_SQL,
    POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL,
    POSTGRES_SELLER_ACCOUNTS_MIGRATIONS,
    POSTGRES_SETTLEMENT_MIGRATIONS,
    PostgresControlPlaneState,
    PostgresSellerAccountResolver,
    PostgresSettlementStore,
    postgres_store_from_env,
)
from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.providers.exact_evm import HostedExactProvider
from hosted_facilitator.hosted.reconciliation import (
    HostedSettlementReconciler,
    ReconciliationOutcome,
    ReconciliationStatus,
)
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.runner import HostedReconciliationRunner
from hosted_facilitator.hosted.schemas import (
    FacilitatorEnvelope,
    ProviderSettlementStatus,
    SettleOutcome,
    VerifyOutcome,
)
from hosted_facilitator.hosted.storage import (
    SettlementAttemptStatus,
    SettlementClaim,
    SettlementStatus,
    utcnow,
)
from hosted_facilitator.hosted.tenancy import (
    PaymentProfileConfig,
    SellerAccountConfig,
    StaticSellerAccountResolver,
    seller_key_hash,
)

pytestmark = pytest.mark.live_postgres

DEFAULT_TEST_POSTGRES_DSN = "postgresql://omniclaw:omniclaw@127.0.0.1:15432/omniclaw"


def _psycopg():
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        pytest.fail(
            "Live Postgres tests require psycopg. Install dev dependencies with "
            "`uv sync --extra dev` before running the suite.",
            pytrace=False,
        )
        raise exc
    return psycopg


@pytest.fixture(scope="session")
def live_postgres_dsn() -> str:
    dsn = os.getenv("OMNICLAW_TEST_POSTGRES_DSN", DEFAULT_TEST_POSTGRES_DSN).strip()
    psycopg = _psycopg()
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.fail(
            "Live Postgres tests require a reachable database. Set "
            "OMNICLAW_TEST_POSTGRES_DSN or start the local compose Postgres with "
            "`docker compose -f docker-compose.yml -f "
            "infra/compose/docker-compose.e2e-ports.yml up -d postgres`. "
            f"Default DSN: {DEFAULT_TEST_POSTGRES_DSN}. Error: {exc}",
            pytrace=False,
        )
    return dsn


@pytest.fixture
def live_schema_dsn(live_postgres_dsn: str):
    psycopg = _psycopg()
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = f"omniclaw_hosted_test_{uuid4().hex}"
    admin = psycopg.connect(live_postgres_dsn, autocommit=True)
    try:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    finally:
        admin.close()

    dsn = make_conninfo(live_postgres_dsn, options=f"-c search_path={schema}")
    try:
        yield dsn
    finally:
        cleanup = psycopg.connect(live_postgres_dsn, autocommit=True)
        try:
            cleanup.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
        finally:
            cleanup.close()


class _FailingAttemptStore(PostgresSettlementStore):
    def _fetchone(self, sql: str, params=None):
        if INSERT_ATTEMPT_SQL in sql:
            raise RuntimeError("forced attempt insert failure")
        return super()._fetchone(sql, params)


class _FailingAttemptClosureStore(PostgresSettlementStore):
    def _execute(self, sql: str, params=None):
        normalized_sql = " ".join(str(sql).split()).upper()
        if normalized_sql.startswith("UPDATE SETTLEMENT_ATTEMPTS"):
            raise RuntimeError("forced attempt closure failure")
        return super()._execute(sql, params)


class _FailingProjectAuditResolver(PostgresSellerAccountResolver):
    def _fetchone(self, sql: str, params=None):
        normalized_sql = " ".join(str(sql).split()).upper()
        if normalized_sql.startswith("INSERT INTO CONTROL_PLANE_AUDIT_EVENTS"):
            raise RuntimeError("forced project audit failure")
        return super()._fetchone(sql, params)


class _LiveProvider(HostedFacilitatorProvider):
    name = "exact_evm"

    def __init__(self, outcome: SettleOutcome | Exception, *, delay: float = 0):
        self._outcome = outcome
        self._delay = delay
        self.settle_calls = 0
        self._lock = Lock()

    async def supported(self, context: RequestContext) -> list[dict]:
        return [
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            }
        ]

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        return VerifyOutcome(isValid=True, payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        with self._lock:
            self.settle_calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome.model_copy(deep=True)


class _ReconcilingLiveProvider(_LiveProvider):
    def __init__(self, reconciliation_outcome: ReconciliationOutcome):
        super().__init__(SettleOutcome(success=True, transaction="0xunused"))
        self.reconciliation_outcome = reconciliation_outcome
        self.reconcile_calls = 0

    async def reconcile_settlement(self, record):
        self.reconcile_calls += 1
        return self.reconciliation_outcome


class _AllowAllOperationsAuthorizer:
    hosted_safe = True

    async def authorize(self, authorization_header, permission, obj=None):
        del authorization_header, permission, obj
        return Principal(
            subject="ops-user", issuer="https://idp.example.test", email="ops@example.test"
        )


def _age_record(store: PostgresSettlementStore, record_id: int | None, updated_at) -> None:
    assert record_id is not None
    store._execute(
        "UPDATE settlement_records SET updated_at = %s WHERE id = %s",
        (updated_at, record_id),
    )
    store._commit()


def _transfer_receipt(
    *,
    status: int = 1,
    transaction: str = "0xarc-worker-settled",
    asset: str = "0x3600000000000000000000000000000000000000",
    payer: str = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    pay_to: str = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    amount: int = 250000,
) -> dict:
    return {
        "status": status,
        "transactionHash": transaction,
        "logs": [
            {
                "address": asset,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + payer[2:],
                    "0x" + "0" * 24 + pay_to[2:],
                ],
                "data": hex(amount),
            }
        ],
    }


def _envelope(resource: str = "https://seller.example.com/private/live") -> FacilitatorEnvelope:
    return FacilitatorEnvelope.model_validate(
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
                    "maxTimeoutSeconds": 300,
                    "extra": {"name": "USDC", "version": "2"},
                },
            },
            "paymentRequirements": {
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x3600000000000000000000000000000000000000",
                "amount": "250000",
                "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "maxTimeoutSeconds": 300,
                "resource": resource,
                "description": "live engine proof",
                "mimeType": "application/json",
                "extra": {"name": "USDC", "version": "2"},
            },
        }
    )


def _context(trace_id: str = "tr_live_engine") -> RequestContext:
    return RequestContext(
        trace_id=trace_id,
        seller_account=SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",)),
    )


def _engine(
    live_schema_dsn: str,
    provider: HostedFacilitatorProvider,
) -> tuple[HostedFacilitatorEngine, PostgresSettlementStore]:
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    return HostedFacilitatorEngine(router=ProviderRouter([provider]), store=store), store


def _store_factory(live_schema_dsn: str, *, initialize: bool = False):
    store = PostgresSettlementStore(live_schema_dsn)
    if initialize:
        store.initialize()
    return store


def _claim(
    store: PostgresSettlementStore,
    trace_id: str = "tr_live",
    fingerprint: str = "fp-live",
    seller_account_id: str = "default",
):
    return store.claim_and_start_settle_attempt(
        seller_account_id=seller_account_id,
        fingerprint=fingerprint,
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id=trace_id,
        raw_requirements={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "amount": "250000",
            "payTo": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "resource": "https://seller.example.com/private/live",
            "description": "private live proof",
            "extra": {"name": "USDC", "secret": "do-not-store"},
        },
    )


def test_live_postgres_migrates_legacy_project_settlement_schema(live_schema_dsn: str):
    psycopg = _psycopg()
    with psycopg.connect(live_schema_dsn, autocommit=True) as conn:
        conn.execute(POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL)
        conn.execute(HOSTED_SCHEMA_MIGRATIONS_SQL)
        conn.execute(
            INSERT_APPLIED_MIGRATION_SQL,
            (
                "settlement",
                "202605170001",
                "create hosted settlement records and attempts",
                "aa4b4719822ff7e9b96799a14e9a6c1d2e28e48ec419ca62b275497efd8b1375",
                datetime.now(timezone.utc),
            ),
        )
        rows = [
            ("project-a", "same-provider-fingerprint", "trace-a"),
            ("project-b", "same-provider-fingerprint", "trace-b"),
        ]
        for project_id, fingerprint, trace_id in rows:
            record_id = conn.execute(
                """
                INSERT INTO settlement_records (
                    project_id, fingerprint, provider, scheme, network, status, trace_id,
                    raw_requirements_json, created_at, updated_at
                )
                VALUES (%s, %s, 'exact_evm', 'exact', 'eip155:5042002',
                    'settle_in_progress', %s, '{}'::jsonb, NOW(), NOW())
                RETURNING id
                """,
                (project_id, fingerprint, trace_id),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO settlement_attempts (
                    settlement_record_id, trace_id, status, started_at
                )
                VALUES (%s, %s, 'started', NOW())
                """,
                (record_id, trace_id),
            )

    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()

    assert store.health_check() is True
    assert store.get("project-a", "same-provider-fingerprint") is not None
    assert store.get("project-b", "same-provider-fingerprint") is not None
    assert store.attempt_count() == 2

    with psycopg.connect(live_schema_dsn) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'settlement_records'
                """
            )
        }
        applied = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT version, checksum
                FROM hosted_schema_migrations
                WHERE scope = 'settlement'
                """
            )
        }
        profile_values = {
            row[0]
            for row in conn.execute("SELECT DISTINCT payment_profile_id FROM settlement_records")
        }
        duplicate_provider_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM settlement_records
            WHERE provider = 'exact_evm'
              AND scheme = 'exact'
              AND network = 'eip155:5042002'
              AND fingerprint = 'same-provider-fingerprint'
            """
        ).fetchone()[0]
        stale_provider_unique_index = conn.execute(
            """
            SELECT COUNT(*)
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'settlement_records'
              AND indexname = 'uniq_settlement_records_provider_fingerprint'
            """
        ).fetchone()[0]
        lookup_index_definition = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND tablename = 'settlement_records'
              AND indexname = 'idx_settlement_records_provider_fingerprint_lookup'
            """
        ).fetchone()[0]

    assert "project_id" not in columns
    assert "seller_account_id" in columns
    assert profile_values == {"default"}
    assert duplicate_provider_rows == 2
    assert stale_provider_unique_index == 0
    assert "seller_account_id" in lookup_index_definition
    assert "payment_profile_id" not in lookup_index_definition
    assert set(applied) == {migration.version for migration in POSTGRES_SETTLEMENT_MIGRATIONS}
    assert (
        applied["202605170001"]
        == "aa4b4719822ff7e9b96799a14e9a6c1d2e28e48ec419ca62b275497efd8b1375"
    )

    replay_a, claim_a, attempt_a = store.claim_and_start_settle_attempt(
        seller_account_id="project-a",
        fingerprint="same-provider-fingerprint",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="trace-replay-a",
        raw_requirements={"resource": "https://seller.example.com/private/a"},
    )
    replay_b, claim_b, attempt_b = store.claim_and_start_settle_attempt(
        seller_account_id="project-b",
        fingerprint="same-provider-fingerprint",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        trace_id="trace-replay-b",
        raw_requirements={"resource": "https://seller.example.com/private/b"},
    )

    assert replay_a.seller_account_id == "project-a"
    assert replay_b.seller_account_id == "project-b"
    assert claim_a == SettlementClaim.DUPLICATE_IN_FLIGHT
    assert claim_b == SettlementClaim.DUPLICATE_IN_FLIGHT
    assert attempt_a is None
    assert attempt_b is None
    store.close()


def test_live_postgres_migrates_project_scoped_seller_accounts(live_schema_dsn: str):
    psycopg = _psycopg()
    api_key = "legacy-project-secret"
    with psycopg.connect(live_schema_dsn, autocommit=True) as conn:
        conn.execute(HOSTED_SCHEMA_MIGRATIONS_SQL)
        conn.execute(
            """
            CREATE TABLE hosted_tenants (
                id TEXT PRIMARY KEY,
                name TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE hosted_projects (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT,
                environment TEXT NOT NULL,
                status TEXT NOT NULL,
                enabled_networks_json JSONB NOT NULL,
                enabled_schemes_json JSONB NOT NULL,
                enabled_providers_json JSONB NOT NULL,
                allowed_assets_json JSONB NOT NULL,
                allowed_pay_to_json JSONB NOT NULL,
                rate_limits_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE hosted_project_api_keys (
                key_hash TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                key_prefix TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                revoked_at TIMESTAMPTZ
            )
            """
        )
        conn.execute(
            """
            INSERT INTO hosted_tenants (id, name, status, created_at, updated_at)
            VALUES ('tenant-legacy', 'Legacy Tenant', 'active', NOW(), NOW())
            """
        )
        conn.execute(
            """
            INSERT INTO hosted_projects (
                id, tenant_id, name, environment, status, enabled_networks_json,
                enabled_schemes_json, enabled_providers_json, allowed_assets_json,
                allowed_pay_to_json, rate_limits_json, created_at, updated_at
            )
            VALUES (
                'legacy-project', 'tenant-legacy', 'Legacy Project', 'alpha', 'active',
                '["eip155:5042002"]'::jsonb,
                '["exact"]'::jsonb,
                '["exact_evm"]'::jsonb,
                '["0x3600000000000000000000000000000000000000"]'::jsonb,
                '["0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"]'::jsonb,
                '{"projectRequestsPerMinute": 77}'::jsonb,
                NOW(), NOW()
            )
            """
        )
        conn.execute(
            """
            INSERT INTO hosted_project_api_keys (
                key_hash, project_id, key_prefix, status, created_at, revoked_at
            )
            VALUES (%s, 'legacy-project', 'omck_legacy', 'active', NOW(), NULL)
            """,
            (seller_key_hash(api_key),),
        )
        conn.execute(
            INSERT_APPLIED_MIGRATION_SQL,
            (
                "seller_accounts",
                "202605170001",
                "create hosted tenants, seller accounts, and seller API keys",
                "66a51f11deff8111ac18a037f21374f230e35b3f99dfc623e5428a59cecfc285",
                datetime.now(timezone.utc),
            ),
        )

    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()

    with pytest.raises(PermissionError, match="Invalid facilitator API key"):
        resolver.resolve(authorization=f"Bearer {api_key}", require_auth=True)
    resolved = resolver.get_seller_account("legacy-project")
    assert resolved is not None
    profile = resolved.default_payment_profile()

    with psycopg.connect(live_schema_dsn) as conn:
        applied = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT version, checksum
                FROM hosted_schema_migrations
                WHERE scope = 'seller_accounts'
                """
            )
        }
        imported_keys = conn.execute(
            "SELECT count(*) FROM hosted_seller_api_keys WHERE key_id LIKE 'migrated_%'"
        ).fetchone()[0]

    assert resolved.seller_account_id == "legacy-project"
    assert profile.payment_profile_id == "default"
    assert profile.rate_limits.seller_requests_per_minute == 77
    assert imported_keys == 0
    assert set(applied) == {migration.version for migration in POSTGRES_SELLER_ACCOUNTS_MIGRATIONS}
    assert applied["202605170001"] == (
        "66a51f11deff8111ac18a037f21374f230e35b3f99dfc623e5428a59cecfc285"
    )
    resolver.close()


def test_live_postgres_schema_jsonb_and_restart_persistence(live_schema_dsn: str):
    first = PostgresSettlementStore(live_schema_dsn)
    assert first.hosted_safe is True
    first.initialize()

    record, claim, attempt_id = _claim(first)
    first.close()

    second = PostgresSettlementStore(live_schema_dsn)
    persisted = second.get("default", "fp-live")

    assert claim == SettlementClaim.CLAIMED
    assert attempt_id is not None
    assert record.raw_requirements["metadataRedacted"] is True
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLE_IN_PROGRESS
    assert persisted.raw_requirements["metadataRedacted"] is True
    assert "resource" not in persisted.raw_requirements
    assert "description" not in persisted.raw_requirements
    assert persisted.raw_requirements["extra"] == {"name": "USDC"}
    assert "resourceHash" in persisted.raw_requirements
    assert second.count() == 1
    assert second.attempt_count() == 1
    second.close()


def test_live_postgres_control_plane_state_round_trip(live_schema_dsn: str):
    state = PostgresControlPlaneState(live_schema_dsn)
    state.initialize()

    assert state.hosted_safe is True
    assert state.durable is True
    assert state.writes_enabled is True
    assert state.health_check() is True
    assert state.allows(provider="exact_evm", network="eip155:5042002") is True

    event = state.set_pause(
        target_type=ControlPlaneTargetType.PROVIDER,
        target="exact_evm",
        paused=True,
        reason="rotate leaked sk_live_ABC123",
        actor="ops@example.com",
        correlation_id="cp-live-1",
    )

    assert event.before is False
    assert event.after is True
    assert event.reason == "operator_supplied"
    assert event.actor == "ops@example.com"
    assert event.correlation_id == "cp-live-1"
    assert state.allows(provider="exact_evm", network="eip155:5042002") is False
    assert state.pause_state().providers == {"exact_evm"}
    audit_tail = state.audit_tail()
    assert len(audit_tail) == 1
    assert audit_tail[0].reason == "operator_supplied"

    resumed = state.set_pause(
        target_type=ControlPlaneTargetType.PROVIDER,
        target="exact_evm",
        paused=False,
        reason="provider restored",
        actor="ops@example.com",
        correlation_id="cp-live-2",
    )

    assert resumed.before is True
    assert resumed.after is False
    assert state.allows(provider="exact_evm", network="eip155:5042002") is True
    assert state.pause_state().providers == set()
    assert [event.after for event in state.audit_tail()] == [False, True]
    state.close()


def test_live_postgres_control_plane_migrates_legacy_audit_targets(live_schema_dsn: str):
    psycopg = _psycopg()
    with psycopg.connect(live_schema_dsn) as conn:
        conn.execute(HOSTED_SCHEMA_MIGRATIONS_SQL)
        conn.execute(POSTGRES_CONTROL_PLANE_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO control_plane_audit_events (
                action, target_type, target, before_paused, after_paused,
                reason, actor, correlation_id, created_at
            )
            VALUES
                ('seller_create', 'project', 'legacy-seller', FALSE, TRUE,
                 'api_key_prefix:omck_legacy', 'ops@example.com', 'cp-legacy', NOW()),
                ('pause_set', 'invalid', 'bad-target', FALSE, TRUE,
                 'operator maintenance', 'ops@example.com', 'cp-invalid', NOW())
            """
        )
        conn.execute(
            INSERT_APPLIED_MIGRATION_SQL,
            (
                "control_plane",
                "202605170001",
                "create hosted control-plane pause and audit tables",
                POSTGRES_CONTROL_PLANE_MIGRATIONS[0].checksum,
                datetime.now(timezone.utc),
            ),
        )
        conn.commit()

    state = PostgresControlPlaneState(live_schema_dsn)
    state.initialize()
    audit_tail = state.audit_tail()
    state.close()

    with psycopg.connect(live_schema_dsn) as conn:
        target_counts = dict(
            conn.execute(
                "SELECT target_type, count(*) FROM control_plane_audit_events GROUP BY target_type"
            ).fetchall()
        )
        has_constraint = conn.execute(
            """
            SELECT count(*)
            FROM pg_constraint
            WHERE conname = 'control_plane_audit_events_target_type_check'
            """
        ).fetchone()[0]

    assert [event.target_type for event in audit_tail] == [ControlPlaneTargetType.SELLER]
    assert target_counts == {"seller": 1}
    assert has_constraint == 1


def test_live_postgres_control_plane_accepts_settlement_audit_after_migration(
    live_schema_dsn: str,
):
    state = PostgresControlPlaneState(live_schema_dsn)
    state.initialize()

    event = state.record_reconciliation_event(
        action="reconciliation_claim",
        record_id=42,
        before=False,
        after=True,
        reason="operator triage",
        actor="ops@example.com",
        correlation_id="rec-live-audit",
    )
    state.close()

    assert event.target_type == ControlPlaneTargetType.SETTLEMENT
    assert event.target == "42"


def test_live_postgres_seller_account_resolver_round_trip_and_revocation(live_schema_dsn: str):
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()

    seller_account = resolver.create_seller_account(
        seller_account_id="seller-live",
        tenant_id="tenant-live",
        name="Live Seller Account",
        enabled_networks=("eip155:5042002",),
        enabled_schemes=("exact",),
        enabled_providers=("exact_evm",),
        allowed_assets=("0x3600000000000000000000000000000000000000",),
        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
    )
    api_key = resolver.issue_api_key(seller_account.seller_account_id)

    resolved_seller_account = resolver.resolve(
        authorization=f"Bearer {api_key.key}", require_auth=True
    )

    assert resolver.health_check() is True
    assert resolved_seller_account.seller_account_id == "seller-live"
    profile = resolved_seller_account.default_payment_profile()
    assert profile.allowed_assets == ("0x3600000000000000000000000000000000000000",)
    assert profile.allowed_pay_to == ("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",)
    assert profile.rate_limits.settle_per_minute == 60

    resolver.revoke_api_key_hash(seller_key_hash(api_key.key))
    with pytest.raises(PermissionError):
        resolver.resolve(authorization=f"Bearer {api_key.key}", require_auth=True)

    resolver.close()


def test_live_postgres_atomic_seller_api_key_creation_and_separate_audit(live_schema_dsn: str):
    psycopg = _psycopg()
    control_state = PostgresControlPlaneState(live_schema_dsn)
    control_state.initialize()
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()

    project, issued = resolver.create_seller_account_with_api_key(
        seller_account_id="atomic-project-live",
        tenant_id="tenant-live",
        name="Atomic Live Project",
        enabled_networks=("eip155:5042002",),
        enabled_schemes=("exact",),
        enabled_providers=("exact_evm",),
        allowed_assets=("0x3600000000000000000000000000000000000000",),
        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
    )
    audit_event = control_state.record_seller_create(
        seller_account_id=project.seller_account_id,
        key_prefix=issued.key_prefix,
        actor="ops@example.com",
        correlation_id="cp-atomic-live",
    )

    assert project.seller_account_id == "atomic-project-live"
    assert issued.key.startswith("omck_")
    assert audit_event.action == "seller_create"
    assert audit_event.target == "atomic-project-live"
    assert audit_event.reason == f"api_key_prefix:{issued.key_prefix}"
    assert resolver.resolve(
        authorization=f"Bearer {issued.key}", require_auth=True
    ).seller_account_id == (project.seller_account_id)

    conn = psycopg.connect(live_schema_dsn)
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM hosted_seller_accounts WHERE id = 'atomic-project-live') AS projects,
              (SELECT count(*) FROM hosted_seller_api_keys WHERE seller_account_id = 'atomic-project-live') AS api_keys,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE action = 'seller_create' AND target = 'atomic-project-live') AS audit_events
            """
        ).fetchone()
    finally:
        conn.close()
        control_state.close()
        resolver.close()

    assert counts == (1, 1, 1)


def test_live_postgres_seller_create_writes_audit_atomically_from_seller_account_db(
    live_schema_dsn: str,
):
    psycopg = _psycopg()
    control_state = PostgresControlPlaneState(live_schema_dsn)
    control_state.initialize()
    control_state.close()
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()

    project, issued, audit_event = resolver.create_seller_account_with_api_key(
        seller_account_id="atomic-no-audit-live",
        tenant_id="tenant-live",
        name="Atomic No Audit Live",
        enabled_networks=("eip155:5042002",),
        enabled_schemes=("exact",),
        enabled_providers=("exact_evm",),
        allowed_assets=("0x3600000000000000000000000000000000000000",),
        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
        audit_actor="ops@example.com",
        audit_correlation_id="cp-atomic-no-audit-live",
    )
    assert project.seller_account_id == "atomic-no-audit-live"
    assert audit_event.action == "seller_create"
    assert audit_event.reason == f"api_key_prefix:{issued.key_prefix}"
    resolver.close()

    conn = psycopg.connect(live_schema_dsn)
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM hosted_seller_accounts WHERE id = 'atomic-no-audit-live') AS projects,
              (SELECT count(*) FROM hosted_seller_api_keys WHERE seller_account_id = 'atomic-no-audit-live') AS api_keys,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE action = 'seller_create' AND target = 'atomic-no-audit-live') AS audit_events
            """
        ).fetchone()
    finally:
        conn.close()

    assert counts == (1, 1, 1)


def test_live_postgres_api_key_issue_and_revoke_write_audit_atomically(
    live_schema_dsn: str,
):
    psycopg = _psycopg()
    control_state = PostgresControlPlaneState(live_schema_dsn)
    control_state.initialize()
    control_state.close()
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()
    resolver.create_seller_account_with_api_key(
        seller_account_id="atomic-key-lifecycle-live",
        tenant_id="tenant-live",
        name="Atomic Key Lifecycle Live",
        enabled_networks=("eip155:5042002",),
        enabled_schemes=("exact",),
        enabled_providers=("exact_evm",),
        allowed_assets=("0x3600000000000000000000000000000000000000",),
        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
    )

    issued, issue_audit = resolver.issue_api_key_with_audit(
        seller_account_id="atomic-key-lifecycle-live",
        payment_profile_id="default",
        actor="ops@example.com",
        correlation_id="cp-key-issue-live",
    )
    key_summary, revoke_audit = resolver.revoke_api_key_with_audit(
        seller_account_id="atomic-key-lifecycle-live",
        key_id=issued.key_id,
        reason="qa key rotation",
        actor="ops@example.com",
        correlation_id="cp-key-revoke-live",
    )
    resolver.close()

    conn = psycopg.connect(live_schema_dsn)
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM hosted_seller_api_keys
               WHERE seller_account_id = 'atomic-key-lifecycle-live'
                 AND key_id = %s
                 AND status = 'revoked') AS revoked_keys,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE action = 'seller_api_key_issue'
                 AND target = 'atomic-key-lifecycle-live') AS issue_audits,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE action = 'seller_api_key_revoke'
                 AND target = 'atomic-key-lifecycle-live') AS revoke_audits
            """,
            (issued.key_id,),
        ).fetchone()
    finally:
        conn.close()

    assert issue_audit.action == "seller_api_key_issue"
    assert revoke_audit.action == "seller_api_key_revoke"
    assert key_summary["status"] == "revoked"
    assert counts == (1, 1, 1)


def test_live_postgres_seller_create_rolls_back_when_atomic_audit_fails(
    live_schema_dsn: str,
):
    psycopg = _psycopg()
    control_state = PostgresControlPlaneState(live_schema_dsn)
    control_state.initialize()
    control_state.close()
    resolver = _FailingProjectAuditResolver(live_schema_dsn)
    resolver.initialize()

    with pytest.raises(RuntimeError, match="forced project audit failure"):
        resolver.create_seller_account_with_api_key(
            seller_account_id="atomic-audit-rollback-live",
            tenant_id="tenant-live",
            name="Atomic Audit Rollback Live",
            enabled_networks=("eip155:5042002",),
            enabled_schemes=("exact",),
            enabled_providers=("exact_evm",),
            allowed_assets=("0x3600000000000000000000000000000000000000",),
            allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
            audit_actor="ops@example.com",
            audit_correlation_id="cp-audit-rollback-live",
        )
    resolver.close()

    conn = psycopg.connect(live_schema_dsn)
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM hosted_seller_accounts
               WHERE id = 'atomic-audit-rollback-live') AS sellers,
              (SELECT count(*) FROM hosted_seller_api_keys
               WHERE seller_account_id = 'atomic-audit-rollback-live') AS api_keys,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE target = 'atomic-audit-rollback-live') AS audit_events
            """
        ).fetchone()
    finally:
        conn.close()

    assert counts == (0, 0, 0)


def test_live_postgres_concurrent_atomic_seller_create_has_one_winner(live_schema_dsn: str):
    control_state = PostgresControlPlaneState(live_schema_dsn)
    control_state.initialize()
    control_state.close()
    initializer = PostgresSellerAccountResolver(live_schema_dsn)
    initializer.initialize()
    initializer.close()
    barrier = Barrier(8)

    def worker(index: int):
        resolver = PostgresSellerAccountResolver(live_schema_dsn)
        try:
            barrier.wait(timeout=10)
            return resolver.create_seller_account_with_api_key(
                seller_account_id="atomic-race-live",
                tenant_id="tenant-live",
                name="Atomic Race Live",
                enabled_networks=("eip155:5042002",),
                enabled_schemes=("exact",),
                enabled_providers=("exact_evm",),
                allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
            )
        except Exception as exc:
            return exc
        finally:
            resolver.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))

    successes = [result for result in results if not isinstance(result, Exception)]
    duplicate_errors = [result for result in results if isinstance(result, ValueError)]

    verifier = PostgresSellerAccountResolver(live_schema_dsn)
    project = verifier.get_seller_account("atomic-race-live")
    verifier.close()
    psycopg = _psycopg()
    conn = psycopg.connect(live_schema_dsn)
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM hosted_seller_accounts WHERE id = 'atomic-race-live') AS projects,
              (SELECT count(*) FROM hosted_seller_api_keys WHERE seller_account_id = 'atomic-race-live') AS api_keys,
              (SELECT count(*) FROM control_plane_audit_events
               WHERE action = 'seller_create' AND target = 'atomic-race-live') AS audit_events
            """
        ).fetchone()
    finally:
        conn.close()

    assert len(successes) == 1
    assert len(duplicate_errors) == 7
    project, issued = successes[0]
    assert project.seller_account_id == "atomic-race-live"
    verifier = PostgresSellerAccountResolver(live_schema_dsn)
    assert verifier.resolve(
        authorization=f"Bearer {issued.key}", require_auth=True
    ).seller_account_id == (project.seller_account_id)
    verifier.close()
    assert project is not None
    assert counts == (1, 1, 0)


def test_live_postgres_seller_account_resolver_rejects_disabled_tenant(live_schema_dsn: str):
    psycopg = _psycopg()
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()
    seller_account = resolver.create_seller_account(
        seller_account_id="seller-disabled-tenant", tenant_id="tenant-disabled"
    )
    api_key = resolver.issue_api_key(seller_account.seller_account_id)

    conn = psycopg.connect(live_schema_dsn, autocommit=True)
    try:
        conn.execute("UPDATE hosted_tenants SET status = 'disabled' WHERE id = 'tenant-disabled'")
    finally:
        conn.close()

    with pytest.raises(PermissionError):
        resolver.resolve(authorization=f"Bearer {api_key.key}", require_auth=True)

    resolver.close()


def test_live_postgres_seller_account_resolver_rejects_disabled_payment_profile(
    live_schema_dsn: str,
):
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()
    resolver.bootstrap_seller_account(
        SellerAccountConfig(
            seller_account_id="seller-disabled-profile",
            tenant_id="tenant-live",
            payment_profiles=(
                PaymentProfileConfig(
                    payment_profile_id="disabled-profile",
                    seller_account_id="seller-disabled-profile",
                    status="disabled",
                    enabled_providers=("exact_evm",),
                    api_keys=("disabled-profile-key",),
                ),
            ),
        )
    )

    with pytest.raises(PermissionError):
        resolver.resolve_access(
            authorization="Bearer disabled-profile-key",
            require_auth=True,
        )
    with pytest.raises(ValueError, match="Payment profile is not active"):
        resolver.issue_api_key("seller-disabled-profile", "disabled-profile")

    resolver.close()


def test_live_postgres_seller_account_resolver_issues_profile_specific_key(
    live_schema_dsn: str,
):
    resolver = PostgresSellerAccountResolver(live_schema_dsn)
    resolver.initialize()
    resolver.bootstrap_seller_account(
        SellerAccountConfig(
            seller_account_id="seller-profile-key-live",
            tenant_id="tenant-live",
            payment_profiles=(
                PaymentProfileConfig(
                    payment_profile_id="profile-a",
                    seller_account_id="seller-profile-key-live",
                    name="Profile A",
                    enabled_networks=("eip155:5042002",),
                    enabled_providers=("exact_evm",),
                    allowed_pay_to=("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
                ),
                PaymentProfileConfig(
                    payment_profile_id="profile-b",
                    seller_account_id="seller-profile-key-live",
                    name="Profile B",
                    enabled_networks=("eip155:5042002",),
                    enabled_providers=("circle_gateway",),
                    allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
                ),
            ),
        )
    )

    issued = resolver.issue_api_key("seller-profile-key-live", "profile-b")
    access = resolver.resolve_access(
        authorization=f"Bearer {issued.key}",
        require_auth=True,
    )

    assert issued.payment_profile_id == "profile-b"
    assert access.seller_account.seller_account_id == "seller-profile-key-live"
    assert access.payment_profile.payment_profile_id == "profile-b"
    assert access.payment_profile.enabled_providers == ("circle_gateway",)
    assert access.payment_profile.allowed_pay_to == ("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",)

    resolver.close()


def test_live_postgres_concurrent_duplicate_claim_creates_one_attempt(live_schema_dsn: str):
    initializer = PostgresSettlementStore(live_schema_dsn)
    initializer.initialize()
    initializer.close()

    def worker(index: int):
        store = PostgresSettlementStore(live_schema_dsn)
        try:
            return _claim(store, trace_id=f"tr_live_{index}")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(8)))

    claims = [claim for _, claim, _ in results]
    attempt_ids = [attempt_id for _, _, attempt_id in results if attempt_id is not None]

    verifier = PostgresSettlementStore(live_schema_dsn)
    assert claims.count(SettlementClaim.CLAIMED) == 1
    assert claims.count(SettlementClaim.DUPLICATE_IN_FLIGHT) == 7
    assert len(attempt_ids) == 1
    assert verifier.count() == 1
    assert verifier.attempt_count() == 1
    verifier.close()


def test_live_postgres_claim_rolls_back_when_attempt_insert_fails(live_schema_dsn: str):
    store = _FailingAttemptStore(live_schema_dsn)
    store.initialize()

    with pytest.raises(RuntimeError, match="forced attempt insert failure"):
        _claim(store)
    store.close()

    verifier = PostgresSettlementStore(live_schema_dsn)
    assert verifier.count() == 0
    assert verifier.attempt_count() == 0
    verifier.close()


def test_live_postgres_terminal_update_rejects_stale_transition(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, attempt_id = _claim(store)
    assert attempt_id is not None

    store.mark_settled(record, transaction="0xsettled", payer="0xpayer")
    with pytest.raises(RuntimeError, match="not in an expected status"):
        store.mark_settle_failed(record, error_reason="late failure")

    persisted = store.get("default", "fp-live")
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xsettled"

    next_record, next_claim, next_attempt_id = _claim(
        store,
        trace_id="tr_after_stale",
        fingerprint="fp-after-stale",
    )
    assert next_claim == SettlementClaim.CLAIMED
    assert next_attempt_id is not None
    assert next_record.fingerprint == "fp-after-stale"
    assert store.count() == 2
    assert store.attempt_count() == 2
    store.close()


def test_live_postgres_settlement_detail_methods_return_record_and_attempts(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, claim, attempt_id = _claim(store, trace_id="tr_detail_live", fingerprint="fp-detail")
    assert claim == SettlementClaim.CLAIMED
    assert record.record_id is not None
    assert attempt_id is not None
    transaction = "0x" + "33" * 32
    store.mark_record_and_finish_settle_attempt(
        record,
        attempt_id=attempt_id,
        record_status=SettlementStatus.SETTLED.value,
        attempt_status=SettlementAttemptStatus.SETTLED,
        transaction=transaction,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )
    other_record, other_claim, other_attempt_id = _claim(
        store,
        trace_id="tr_other_detail_live",
        fingerprint="fp-other-detail",
        seller_account_id="other",
    )
    assert other_claim == SettlementClaim.CLAIMED
    assert other_attempt_id is not None
    store.mark_record_and_finish_settle_attempt(
        other_record,
        attempt_id=other_attempt_id,
        record_status=SettlementStatus.SETTLED.value,
        attempt_status=SettlementAttemptStatus.SETTLED,
        transaction="0x" + "55" * 32,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )

    loaded = store.get_record_by_id(record.record_id)
    attempts = store.list_attempts_for_record(record.record_id)

    assert loaded is not None
    assert loaded.record_id == record.record_id
    assert loaded.status == SettlementStatus.SETTLED
    assert loaded.transaction == transaction
    assert loaded.raw_requirements["resourceHash"]
    assert loaded.raw_requirements["extra"] == {"name": "USDC"}
    assert store.settlement_status_counts_for_seller("default") == {"settled": 1}
    assert store.settlement_attempt_count_for_seller("default") == 1
    assert store.settlement_amount_atomic_for_seller("default") == 250000
    assert store.list_recent_records_for_seller("default", limit=5)[0].record_id == record.record_id
    assert store.settlement_status_counts_for_seller("other") == {"settled": 1}
    assert store.settlement_attempt_count_for_seller("other") == 1
    assert store.settlement_amount_atomic_for_seller("other") == 250000
    assert (
        store.list_recent_records_for_seller("other", limit=5)[0].record_id
        == other_record.record_id
    )
    assert len(attempts) == 1
    assert attempts[0].attempt_id == attempt_id
    assert attempts[0].status == SettlementAttemptStatus.SETTLED
    assert attempts[0].transaction == transaction
    store.close()


def test_live_postgres_reconciliation_queue_filters_records(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    unknown, _, _ = _claim(
        store,
        trace_id="tr_queue_unknown_live",
        fingerprint="fp-queue-unknown-live",
        seller_account_id="alpha",
    )
    store.mark_unknown(unknown, error_reason="provider_timeout")
    submitted, _, submitted_attempt_id = _claim(
        store,
        trace_id="tr_queue_submitted_live",
        fingerprint="fp-queue-submitted-live",
        seller_account_id="alpha",
    )
    assert submitted_attempt_id is not None
    store.mark_record_and_finish_settle_attempt(
        submitted,
        attempt_id=submitted_attempt_id,
        record_status=SettlementStatus.SUBMITTED.value,
        attempt_status=SettlementAttemptStatus.SUBMITTED,
        transaction="0xsubmitted-live",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )
    other, _, other_attempt_id = _claim(
        store,
        trace_id="tr_queue_other_live",
        fingerprint="fp-queue-other-live",
        seller_account_id="other",
    )
    assert other_attempt_id is not None
    store.mark_record_and_finish_settle_attempt(
        other,
        attempt_id=other_attempt_id,
        record_status=SettlementStatus.SETTLED.value,
        attempt_status=SettlementAttemptStatus.SETTLED,
        transaction="0x" + "77" * 32,
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )

    queue = store.list_reconciliation_queue(
        statuses=(SettlementStatus.UNKNOWN, SettlementStatus.SUBMITTED),
        stale_before=utcnow() + timedelta(seconds=1),
        seller_account_id="alpha",
        provider="exact_evm",
        network="eip155:5042002",
        limit=10,
    )
    counts = store.reconciliation_queue_status_counts(
        statuses=(SettlementStatus.UNKNOWN, SettlementStatus.SUBMITTED),
        stale_before=utcnow() + timedelta(seconds=1),
        seller_account_id="alpha",
        provider="exact_evm",
        network="eip155:5042002",
    )

    assert [record.record_id for record in queue] == [unknown.record_id, submitted.record_id]
    assert counts == {"submitted": 1, "unknown": 1}
    assert all(record.seller_account_id == "alpha" for record in queue)
    assert all(
        record.status in {SettlementStatus.UNKNOWN, SettlementStatus.SUBMITTED} for record in queue
    )
    store.close()


def test_live_postgres_control_plane_routes_use_store_factory(live_schema_dsn: str):
    initializer = _store_factory(live_schema_dsn, initialize=True)
    record, _, attempt_id = _claim(
        initializer,
        trace_id="tr_route_factory",
        fingerprint="fp-route-factory",
        seller_account_id="alpha",
    )
    assert attempt_id is not None
    initializer.mark_record_and_finish_settle_attempt(
        record,
        attempt_id=attempt_id,
        record_status=SettlementStatus.SUBMITTED.value,
        attempt_status=SettlementAttemptStatus.SUBMITTED,
        transaction="0xsubmitted-route",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason=None,
    )
    unknown_record, _, unknown_attempt_id = _claim(
        initializer,
        trace_id="tr_route_factory_unknown",
        fingerprint="fp-route-factory-unknown",
        seller_account_id="alpha",
    )
    assert unknown_attempt_id is not None
    initializer.mark_record_and_finish_settle_attempt(
        unknown_record,
        attempt_id=unknown_attempt_id,
        record_status=SettlementStatus.UNKNOWN.value,
        attempt_status=SettlementAttemptStatus.UNKNOWN,
        transaction="",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason="provider status unknown",
    )
    manual_record, _, manual_attempt_id = _claim(
        initializer,
        trace_id="tr_route_factory_manual",
        fingerprint="fp-route-factory-manual",
        seller_account_id="alpha",
    )
    assert manual_attempt_id is not None
    initializer.mark_record_and_finish_settle_attempt(
        manual_record,
        attempt_id=manual_attempt_id,
        record_status=SettlementStatus.UNKNOWN.value,
        attempt_status=SettlementAttemptStatus.UNKNOWN,
        transaction="",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason="provider status unknown",
    )
    other_record, _, other_attempt_id = _claim(
        initializer,
        trace_id="tr_route_factory_other",
        fingerprint="fp-route-factory-other",
        seller_account_id="other",
    )
    assert other_attempt_id is not None
    initializer.mark_record_and_finish_settle_attempt(
        other_record,
        attempt_id=other_attempt_id,
        record_status=SettlementStatus.UNKNOWN.value,
        attempt_status=SettlementAttemptStatus.UNKNOWN,
        transaction="",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        error_reason="other seller must not leak",
    )
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    psycopg = _psycopg()
    with psycopg.connect(live_schema_dsn) as conn:
        conn.execute(
            """
            UPDATE settlement_records
            SET updated_at = %s
            WHERE id = %s
            """,
            (stale_cutoff, record.record_id),
        )
        conn.execute(
            """
            UPDATE settlement_records
            SET status = %s,
                error_reason = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                SettlementStatus.MANUAL_REVIEW.value,
                "operator review required",
                datetime.now(timezone.utc),
                manual_record.record_id,
            ),
        )
    initializer.close()
    seller_resolver = PostgresSellerAccountResolver(live_schema_dsn)
    seller_resolver.initialize()
    seller_resolver.create_seller_account_with_api_key(
        seller_account_id="alpha",
        tenant_id="alpha",
        name="Alpha",
        allowed_pay_to=("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
    )
    app = create_hosted_facilitator_app(
        engine=HostedFacilitatorEngine(router=ProviderRouter([])),
        resolver=seller_resolver,
        operations_authorizer=_AllowAllOperationsAuthorizer(),
        store_factory=lambda: _store_factory(live_schema_dsn),
    )

    with TestClient(app) as client:
        queue_response = client.get("/ops/api/reconciliation?status=submitted")
        detail_response = client.get(f"/ops/api/settlements/{record.record_id}")
        seller_response = client.get("/ops/api/sellers/alpha")

    assert queue_response.status_code == 200
    assert queue_response.headers["cache-control"] == "no-store"
    assert queue_response.json()["counts"] == {"submitted": 1}
    assert queue_response.json()["items"][0]["recordId"] == record.record_id
    assert detail_response.status_code == 200
    assert detail_response.headers["cache-control"] == "no-store"
    assert detail_response.json()["record"]["recordId"] == record.record_id
    assert seller_response.status_code == 200
    assert seller_response.headers["cache-control"] == "no-store"
    seller_settlement = seller_response.json()["settlement"]
    assert seller_settlement["records"] == 3
    assert seller_settlement["submitted"] == 1
    assert seller_settlement["unknown"] == 1
    assert seller_settlement["manualReviewBacklog"] == 1
    assert seller_settlement["reconciliationRisk"]["backlog"] == 3
    assert seller_settlement["reconciliationRisk"]["active"] == 1
    assert seller_settlement["reconciliationRisk"]["staleActive"] == 1
    assert seller_settlement["reconciliationRisk"]["manualReview"] == 1
    assert seller_settlement["reconciliationRisk"]["unknown"] == 1
    assert seller_settlement["reconciliationRisk"]["oldestAgeSeconds"] >= 300
    assert seller_settlement["lastUpdatedAt"]
    assert all(record["sellerRef"] == "alpha" for record in seller_settlement["recentRecords"])
    seller_resolver.close()


def test_live_postgres_reconciliation_candidates_use_exclusive_leases(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-reconcile-lease")
    store.mark_unknown(record, error_reason="provider_timeout")

    stale_before = utcnow() + timedelta(seconds=1)
    candidates = store.list_reconciliation_candidates(
        statuses=(SettlementStatus.UNKNOWN,),
        stale_before=stale_before,
        limit=10,
    )
    lease_until = utcnow() + timedelta(minutes=5)
    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=lease_until,
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=stale_before,
    )
    claimed_again = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-b",
        lease_until=utcnow() + timedelta(minutes=10),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=stale_before,
    )
    leased_candidates = store.list_reconciliation_candidates(
        statuses=(SettlementStatus.UNKNOWN,),
        stale_before=stale_before,
        limit=10,
    )

    assert [candidate.record_id for candidate in candidates] == [record.record_id]
    assert claimed is not None
    assert claimed.record_id == record.record_id
    assert claimed_again is None
    assert leased_candidates == []
    store.close()


def test_live_postgres_reconciliation_release_uses_owner_guard(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-reconcile-release")
    store.mark_unknown(record, error_reason="provider_timeout")
    owner = _safe_operator_lease_owner("ops@example.com", issuer="https://idp.example.test")
    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner=owner,
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert claimed is not None

    wrong_owner = store.release_reconciliation_record(
        record_id=record.record_id or 0,
        owner=_safe_operator_lease_owner("other@example.com", issuer="https://idp.example.test"),
    )
    after_wrong_owner = store.get("default", "fp-reconcile-release")
    released = store.release_reconciliation_record(record_id=record.record_id or 0, owner=owner)
    after_release = store.get("default", "fp-reconcile-release")

    assert wrong_owner is None
    assert after_wrong_owner is not None
    assert after_wrong_owner.reconciliation_owner == owner
    assert released is not None
    assert released.reconciliation_owner is None
    assert after_release is not None
    assert after_release.reconciliation_owner is None
    store.close()


def test_live_postgres_reconciliation_control_action_is_atomic_and_audited(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    state = PostgresControlPlaneState(live_schema_dsn)
    state.initialize()
    record, _, _ = _claim(store, fingerprint="fp-reconcile-control-action")
    store.mark_unknown(record, error_reason="provider_timeout")
    owner = _safe_operator_lease_owner("ops@example.com", issuer="https://idp.example.test")

    updated, event = store.apply_reconciliation_control_action(
        action="reconciliation_claim",
        record_id=record.record_id or 0,
        owner=owner,
        lease_until=utcnow() + timedelta(minutes=5),
        active_stale_before=utcnow() - timedelta(minutes=5),
        reason="operator triage",
        actor="ops@example.com",
        correlation_id="rec-live-atomic",
    )
    audit_tail = state.audit_tail(limit=5)

    assert updated.reconciliation_owner == owner
    assert event.target_type == ControlPlaneTargetType.SETTLEMENT
    assert event.target == str(record.record_id)
    assert audit_tail[0].action == "reconciliation_claim"
    assert audit_tail[0].target == str(record.record_id)
    state.close()
    store.close()


def test_live_postgres_reconciliation_stale_attempt_and_terminal_transition(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, attempt_id = _claim(store, fingerprint="fp-reconcile-terminal")
    assert attempt_id is not None

    attempts = store.list_stale_started_attempts(
        started_before=utcnow() + timedelta(seconds=1),
        limit=10,
    )
    store.mark_unknown(record, error_reason="provider_timeout")
    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert claimed is not None

    store.mark_reconciled_settled(
        claimed,
        transaction="0xreconciled",
        payer="0xpayer",
        owner="reconciler-a",
    )
    persisted = store.get("default", "fp-reconcile-terminal")
    duplicate_record, duplicate_claim, duplicate_attempt = _claim(
        store,
        trace_id="tr_after_reconcile",
        fingerprint="fp-reconcile-terminal",
    )

    assert [attempt.attempt_id for attempt in attempts] == [attempt_id]
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xreconciled"
    assert duplicate_record.status == SettlementStatus.SETTLED
    assert duplicate_claim == SettlementClaim.DUPLICATE_FINAL
    assert duplicate_attempt is None
    store.close()


@pytest.mark.parametrize(
    ("source_status", "terminal_status"),
    [
        (SettlementStatus.UNKNOWN, SettlementStatus.SETTLED),
        (SettlementStatus.UNKNOWN, SettlementStatus.SETTLE_FAILED),
        (SettlementStatus.UNKNOWN, SettlementStatus.MANUAL_REVIEW),
        (SettlementStatus.SUBMITTED, SettlementStatus.SETTLED),
        (SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SETTLED),
        (SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SETTLE_FAILED),
    ],
)
def test_live_postgres_reconciliation_terminal_transition_matrix(
    live_schema_dsn: str,
    source_status: SettlementStatus,
    terminal_status: SettlementStatus,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    fingerprint = f"fp-reconcile-{source_status}-{terminal_status}"
    record, _, _ = _claim(store, fingerprint=fingerprint)
    if source_status == SettlementStatus.UNKNOWN:
        store.mark_unknown(record, error_reason="provider_timeout")
    elif source_status == SettlementStatus.SUBMITTED:
        store.mark_submitted(record, transaction="0xsubmitted", payer="0xpayer")

    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(source_status,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert claimed is not None

    if terminal_status == SettlementStatus.SETTLED:
        store.mark_reconciled_settled(
            claimed,
            transaction="0xreconciled",
            payer="0xpayer",
            owner="reconciler-a",
        )
    elif terminal_status == SettlementStatus.SETTLE_FAILED:
        store.mark_reconciled_failed(
            claimed,
            error_reason="chain_reverted",
            owner="reconciler-a",
        )
    else:
        store.mark_manual_review(
            claimed,
            error_reason="ambiguous_chain_state",
            owner="reconciler-a",
        )

    persisted = store.get("default", fingerprint)

    assert persisted is not None
    assert persisted.status == terminal_status
    assert persisted.transaction == (
        "0xreconciled" if terminal_status == SettlementStatus.SETTLED else record.transaction
    )
    store.close()


@pytest.mark.parametrize(
    ("source_status", "terminal_status"),
    [
        (SettlementStatus.SUBMITTED, SettlementStatus.SETTLE_FAILED),
        (SettlementStatus.SUBMITTED, SettlementStatus.MANUAL_REVIEW),
        (SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.MANUAL_REVIEW),
    ],
)
def test_live_postgres_reconciliation_rejects_invalid_protocol_transitions(
    live_schema_dsn: str,
    source_status: SettlementStatus,
    terminal_status: SettlementStatus,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    fingerprint = f"fp-invalid-reconcile-{source_status}-{terminal_status}"
    record, _, _ = _claim(store, fingerprint=fingerprint)
    if source_status == SettlementStatus.SUBMITTED:
        store.mark_submitted(record, transaction="0xsubmitted", payer="0xpayer")

    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(source_status,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert claimed is not None

    with pytest.raises(RuntimeError, match="expected status"):
        if terminal_status == SettlementStatus.SETTLE_FAILED:
            store.mark_reconciled_failed(
                claimed,
                error_reason="chain_reverted",
                owner="reconciler-a",
            )
        else:
            store.mark_manual_review(
                claimed,
                error_reason="ambiguous_chain_state",
                owner="reconciler-a",
            )
    persisted = store.get("default", fingerprint)

    assert persisted is not None
    assert persisted.status == source_status
    store.close()


def test_live_postgres_reconciliation_terminal_update_requires_current_lease_owner(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-reconcile-owner")
    store.mark_unknown(record, error_reason="provider_timeout")
    candidate = store.list_reconciliation_candidates(
        statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
        limit=1,
    )[0]
    claimed = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert claimed is not None
    assert candidate.record_id == claimed.record_id

    with pytest.raises(RuntimeError, match="not leased by reconciler-b"):
        store.mark_reconciled_settled(
            claimed,
            transaction="0xwrong-owner",
            payer="0xpayer",
            owner="reconciler-b",
        )
    after_wrong_owner = store.get("default", "fp-reconcile-owner")
    assert after_wrong_owner is not None
    assert after_wrong_owner.status == SettlementStatus.UNKNOWN

    store.mark_reconciled_settled(
        claimed,
        transaction="0xright-owner",
        payer="0xpayer",
        owner="reconciler-a",
    )
    persisted = store.get("default", "fp-reconcile-owner")

    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xright-owner"
    store.close()


def test_live_postgres_reconciliation_terminal_update_rejects_stale_same_owner_lease(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-reconcile-same-owner")
    store.mark_unknown(record, error_reason="provider_timeout")
    stale_claim = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() - timedelta(seconds=1),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    current_claim = store.claim_reconciliation_record(
        record_id=record.record_id or 0,
        owner="reconciler-a",
        lease_until=utcnow() + timedelta(minutes=5),
        eligible_statuses=(SettlementStatus.UNKNOWN,),
        stale_before=utcnow() + timedelta(seconds=1),
    )
    assert stale_claim is not None
    assert current_claim is not None
    assert stale_claim.reconciliation_lease_until != current_claim.reconciliation_lease_until

    with pytest.raises(RuntimeError, match="expected status"):
        store.mark_reconciled_settled(
            stale_claim,
            transaction="0xstale-owner",
            payer="0xpayer",
            owner="reconciler-a",
        )
    after_stale_owner = store.get("default", "fp-reconcile-same-owner")
    assert after_stale_owner is not None
    assert after_stale_owner.status == SettlementStatus.UNKNOWN

    store.mark_reconciled_settled(
        current_claim,
        transaction="0xcurrent-owner",
        payer="0xpayer",
        owner="reconciler-a",
    )
    persisted = store.get("default", "fp-reconcile-same-owner")

    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xcurrent-owner"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_settles_stale_submitted_record(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-settled")
    store.mark_submitted(record, transaction="0xsubmitted", payer="0xpayer")
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.SETTLED,
            transaction="0xworker-settled",
            payer="0xpayer",
        )
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_submitted_after=timedelta(seconds=0),
        clock=lambda: utcnow() + timedelta(seconds=1),
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-settled")

    assert stats.scanned == 1
    assert stats.claimed == 1
    assert stats.settled == 1
    assert provider.reconcile_calls == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xworker-settled"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciliation_runner_executes_bounded_operational_run(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-runner-settled")
    store.mark_submitted(record, transaction="0xrunner-submitted", payer="0xpayer")
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.SETTLED,
            transaction="0xrunner-settled",
            payer="0xpayer",
        )
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="runner-a",
        stale_submitted_after=timedelta(seconds=0),
        clock=lambda: utcnow() + timedelta(seconds=1),
    )
    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=0,
        max_runs=1,
    )

    loop_stats = await runner.run()
    persisted = store.get("default", "fp-runner-settled")

    assert loop_stats.runs == 1
    assert loop_stats.failed_runs == 0
    assert loop_stats.aggregate.settled == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xrunner-settled"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_marks_stale_submitted_unknown(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-unknown")
    store.mark_submitted(record, transaction="0xsubmitted", payer="0xpayer")
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.UNKNOWN,
            error_reason="receipt_not_found",
        )
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_submitted_after=timedelta(seconds=0),
        clock=lambda: utcnow() + timedelta(seconds=1),
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-unknown")

    assert stats.marked_unknown == 1
    assert provider.reconcile_calls == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.UNKNOWN
    assert persisted.error_reason == "receipt_not_found"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_marks_stale_in_progress_unknown(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-stale-in-progress")
    now = utcnow()
    _age_record(store, record.record_id, now - timedelta(minutes=3))
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(status=ReconciliationStatus.PENDING),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-stale-in-progress")

    assert stats.scanned == 1
    assert stats.claimed == 1
    assert stats.marked_unknown == 1
    assert stats.pending == 0
    assert provider.reconcile_calls == 1
    assert provider.settle_calls == 0
    assert persisted is not None
    assert persisted.status == SettlementStatus.UNKNOWN
    assert persisted.error_reason == "stale_settle_in_progress"
    assert (
        store.list_stale_started_attempts(
            started_before=now + timedelta(seconds=1),
            limit=10,
        )
        == []
    )
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_rolls_back_record_when_attempt_closure_fails(
    live_schema_dsn: str,
):
    store = _FailingAttemptClosureStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-attempt-rollback")
    now = utcnow()
    _age_record(store, record.record_id, now - timedelta(minutes=3))
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(status=ReconciliationStatus.PENDING),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-attempt-rollback")

    assert stats.errors == 1
    assert stats.marked_unknown == 0
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLE_IN_PROGRESS
    assert persisted.error_reason is None
    assert (
        len(
            store.list_stale_started_attempts(
                started_before=now + timedelta(seconds=1),
                limit=10,
            )
        )
        == 1
    )
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_moves_old_unknown_to_manual_review(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-manual")
    store.mark_unknown(record, error_reason="provider_timeout")
    now = utcnow()
    old_updated_at = now - timedelta(minutes=31)
    _age_record(store, record.record_id, old_updated_at)
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([]),
        store=store,
        owner="worker-a",
        stale_unknown_after=timedelta(seconds=0),
        manual_review_after=timedelta(minutes=30),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-manual")

    assert stats.manual_review == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.MANUAL_REVIEW
    assert persisted.error_reason == "reconciliation_provider_not_registered"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_rejects_settled_without_transaction(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-settled-no-proof")
    store.mark_unknown(record, error_reason="provider_timeout")
    now = utcnow()
    _age_record(store, record.record_id, now - timedelta(minutes=6))
    provider = _ReconcilingLiveProvider(ReconciliationOutcome(status=ReconciliationStatus.SETTLED))
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_unknown_after=timedelta(seconds=0),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-settled-no-proof")

    assert stats.claimed == 1
    assert stats.errors == 1
    assert stats.settled == 0
    assert provider.reconcile_calls == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.UNKNOWN
    assert persisted.transaction is None
    assert persisted.reconciliation_owner == "worker-a"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_moves_old_unknown_to_manual_when_hook_missing(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-no-hook")
    store.mark_unknown(record, error_reason="provider_timeout")
    now = utcnow()
    _age_record(store, record.record_id, now - timedelta(minutes=31))
    provider = _LiveProvider(SettleOutcome(success=True, transaction="0xunused"))
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_unknown_after=timedelta(seconds=0),
        manual_review_after=timedelta(minutes=30),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-no-hook")

    assert stats.manual_review == 1
    assert stats.pending == 0
    assert persisted is not None
    assert persisted.status == SettlementStatus.MANUAL_REVIEW
    assert persisted.error_reason == "provider_reconciliation_unavailable"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_reconciler_moves_old_unknown_to_manual_when_provider_pending(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-worker-provider-pending")
    store.mark_unknown(record, error_reason="provider_timeout")
    now = utcnow()
    _age_record(store, record.record_id, now - timedelta(minutes=31))
    provider = _ReconcilingLiveProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.PENDING,
            error_reason="receipt_still_pending",
        )
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_unknown_after=timedelta(seconds=0),
        manual_review_after=timedelta(minutes=30),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-worker-provider-pending")

    assert stats.manual_review == 1
    assert stats.pending == 0
    assert provider.reconcile_calls == 1
    assert persisted is not None
    assert persisted.status == SettlementStatus.MANUAL_REVIEW
    assert persisted.error_reason == "receipt_still_pending"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_exact_evm_reconciler_settles_successful_receipt(live_schema_dsn: str):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-arc-worker-settled")
    store.mark_submitted(
        record,
        transaction="0xarc-worker-settled",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    provider = HostedExactProvider(
        _LiveProvider(SettleOutcome(success=True, transaction="0xunused")),
        receipt_checker=lambda *_: _transfer_receipt(transaction="0xarc-worker-settled"),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_submitted_after=timedelta(seconds=0),
        clock=lambda: utcnow() + timedelta(seconds=1),
    )

    stats = await reconciler.run_once()
    persisted = store.get("default", "fp-arc-worker-settled")

    assert stats.settled == 1
    assert stats.errors == 0
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLED
    assert persisted.transaction == "0xarc-worker-settled"
    store.close()


@pytest.mark.asyncio
async def test_live_postgres_exact_evm_failed_submitted_receipt_moves_through_unknown(
    live_schema_dsn: str,
):
    store = PostgresSettlementStore(live_schema_dsn)
    store.initialize()
    record, _, _ = _claim(store, fingerprint="fp-arc-worker-failed")
    store.mark_submitted(
        record,
        transaction="0xarc-worker-failed",
        payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    now = utcnow()
    provider = HostedExactProvider(
        _LiveProvider(SettleOutcome(success=True, transaction="0xunused")),
        receipt_checker=lambda *_: _transfer_receipt(status=0, transaction="0xarc-worker-failed"),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_submitted_after=timedelta(seconds=0),
        stale_unknown_after=timedelta(seconds=0),
        clock=lambda: now + timedelta(seconds=1),
    )

    first_stats = await reconciler.run_once()
    first = store.get("default", "fp-arc-worker-failed")
    assert first_stats.marked_unknown == 1
    assert first is not None
    assert first.status == SettlementStatus.UNKNOWN
    assert first.transaction == "0xarc-worker-failed"

    _age_record(store, first.record_id, now - timedelta(minutes=6))
    second_stats = await reconciler.run_once()
    persisted = store.get("default", "fp-arc-worker-failed")

    assert second_stats.failed == 1
    assert second_stats.errors == 0
    assert persisted is not None
    assert persisted.status == SettlementStatus.SETTLE_FAILED
    assert persisted.transaction == "0xarc-worker-failed"
    assert persisted.error_reason == "exact_evm_transaction_failed"
    store.close()


def test_live_postgres_http_concurrent_settle_uses_request_scoped_stores(live_schema_dsn: str):
    provider = _LiveProvider(
        SettleOutcome(
            success=True,
            transaction="0xhttp-settled",
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        delay=0.1,
    )
    initializer = _store_factory(live_schema_dsn, initialize=True)
    initializer.close()
    engine = HostedFacilitatorEngine(router=ProviderRouter([provider]))
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=StaticSellerAccountResolver(
            [SellerAccountConfig(seller_account_id="default", api_keys=("secret-key",))]
        ),
        store_factory=lambda: _store_factory(live_schema_dsn),
    )
    envelope = _envelope("https://seller.example.com/private/live-http-concurrent")
    body = envelope.model_dump(by_alias=True)
    barrier = Barrier(8)

    def worker(index: int):
        barrier.wait()
        with TestClient(app) as client:
            return client.post(
                "/settle",
                json=body,
                headers={
                    "Authorization": "Bearer secret-key",
                    "x-request-id": f"tr_live_http_{index}",
                },
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(worker, range(8)))

    payloads = [response.json() for response in responses]
    verifier = PostgresSettlementStore(live_schema_dsn)

    assert {response.status_code for response in responses} == {200}
    assert provider.settle_calls == 1
    assert sum(1 for payload in payloads if payload.get("duplicate") is not True) == 1
    assert sum(1 for payload in payloads if payload.get("duplicate") is True) == 7
    assert {payload.get("errorReason") for payload in payloads if payload.get("duplicate")} <= {
        "settlement_in_progress",
        None,
    }
    assert {payload.get("transaction") for payload in payloads if payload.get("success")} == {
        "0xhttp-settled"
    }
    assert verifier.count() == 1
    assert verifier.attempt_count() == 1
    verifier.close()


@pytest.mark.asyncio
async def test_live_postgres_engine_concurrent_settle_calls_provider_once(live_schema_dsn: str):
    provider = _LiveProvider(
        SettleOutcome(
            success=True,
            transaction="0xengine-settled",
            network="eip155:5042002",
            payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        delay=0.05,
    )
    engine, store = _engine(live_schema_dsn, provider)
    envelope = _envelope("https://seller.example.com/private/live-engine-concurrent")
    context = _context("tr_live_engine_concurrent")

    results = await asyncio.gather(*(engine.settle(envelope, context) for _ in range(8)))

    assert provider.settle_calls == 1
    assert sum(1 for result in results if result.success) == 1
    assert sum(1 for result in results if result.duplicate) == 7
    assert {result.error_reason for result in results if result.duplicate} == {
        "settlement_in_progress"
    }
    assert store.count() == 1
    assert store.attempt_count() == 1
    store.close()


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_error", "expected_transaction"),
    [
        (
            SettleOutcome(
                success=True,
                transaction="0xengine-settled",
                network="eip155:5042002",
                payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ),
            SettlementStatus.SETTLED,
            None,
            "0xengine-settled",
        ),
        (
            SettleOutcome(
                success=False,
                transaction="0xengine-submitted",
                network="eip155:5042002",
                payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                settlementStatus=ProviderSettlementStatus.SUBMITTED,
            ),
            SettlementStatus.SUBMITTED,
            "settlement_submitted",
            "0xengine-submitted",
        ),
        (
            SettleOutcome(
                success=False,
                network="eip155:5042002",
                payer="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                errorReason="provider_result_ambiguous",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
            ),
            SettlementStatus.UNKNOWN,
            "settlement_outcome_unknown",
            None,
        ),
    ],
)
@pytest.mark.asyncio
async def test_live_postgres_engine_persisted_outcome_blocks_resubmission_after_restart(
    live_schema_dsn: str,
    outcome: SettleOutcome,
    expected_status: SettlementStatus,
    expected_error: str | None,
    expected_transaction: str | None,
):
    resource = f"https://seller.example.com/private/live-engine-{expected_status}"
    first_provider = _LiveProvider(outcome)
    first_engine, first_store = _engine(live_schema_dsn, first_provider)
    envelope = _envelope(resource)
    fingerprint = payment_fingerprint(
        payment_payload=envelope.payment_payload,
        payment_requirements=envelope.payment_requirements,
        provider="exact_evm",
        x402_version=envelope.x402_version,
    )

    first = await first_engine.settle(envelope, _context("tr_first"))
    first_store.close()

    second_provider = _LiveProvider(
        SettleOutcome(success=True, transaction="0xshould-not-submit"),
    )
    second_store = PostgresSettlementStore(live_schema_dsn)
    second_engine = HostedFacilitatorEngine(
        router=ProviderRouter([second_provider]),
        store=second_store,
    )
    second = await second_engine.settle(envelope, _context("tr_second"))

    assert first_provider.settle_calls == 1
    assert second_provider.settle_calls == 0
    assert second.duplicate is True
    assert second.settlement_status == (
        ProviderSettlementStatus.SETTLED
        if expected_status == SettlementStatus.SETTLED
        else ProviderSettlementStatus.SUBMITTED
        if expected_status == SettlementStatus.SUBMITTED
        else ProviderSettlementStatus.UNKNOWN
    )
    assert second.error_reason == expected_error
    assert second.transaction == expected_transaction
    assert first.success is (expected_status == SettlementStatus.SETTLED)
    assert second_store.count() == 1
    assert second_store.attempt_count() == 1
    record = second_store.get("default", fingerprint)
    assert record is not None
    assert record.status == expected_status
    assert record.transaction == expected_transaction
    second_store.close()


def test_live_postgres_store_from_env_uses_hosted_safe_dsn(monkeypatch, live_schema_dsn: str):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", live_schema_dsn)
    monkeypatch.setenv("DATABASE_URL", "postgresql://ignored/ignored")

    store = postgres_store_from_env()

    assert store.hosted_safe is True
    store.initialize()
    assert store.count() == 0
    store.close()
