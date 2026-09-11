from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from hosted_facilitator.exact import ExactFacilitatorConfig
from hosted_facilitator.hosted.reconciliation import (
    HostedSettlementReconciler,
    ReconciliationOutcome,
    ReconciliationRunStats,
    ReconciliationStatus,
)
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.runner import (
    HostedExactRuntimeConfig,
    HostedReconciliationRunner,
    _hosted_exact_max_concurrent_settlements,
    create_hosted_exact_facilitator_app_from_env,
    create_hosted_exact_reconciler_from_env,
    load_hosted_exact_runtime_config_from_env,
    reconciliation_owner_from_env,
)
from hosted_facilitator.hosted.signers import HostedSignerConfig, HostedSignerGuard
from hosted_facilitator.hosted.storage import SettlementRecord, SettlementStatus, utcnow
from hosted_facilitator.hosted.tenancy import SellerAccountConfig


class _FakeReconciler:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def run_once(self):
        self.calls += 1
        if not self.results:
            return ReconciliationRunStats()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _ReconcilingProvider:
    name = "exact_evm"

    def __init__(self, outcome: ReconciliationOutcome | Exception):
        self.outcome = outcome
        self.reconcile_calls = 0
        self.settle_calls = 0

    async def reconcile_settlement(self, _record):
        self.reconcile_calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def settle(self, *_args, **_kwargs):
        self.settle_calls += 1
        raise AssertionError("reconciler must not call provider settlement")


class _ReconciliationStore:
    def __init__(self, record: SettlementRecord):
        self.record = record

    def list_reconciliation_candidates(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before,
        limit: int,
    ):
        if limit <= 0 or self.record.status not in statuses:
            return []
        if self.record.updated_at > stale_before:
            return []
        return [self.record]

    def claim_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
        lease_until,
        eligible_statuses: tuple[SettlementStatus, ...],
        stale_before,
    ):
        if self.record.record_id != record_id or self.record.status not in eligible_statuses:
            return None
        if stale_before is not None and self.record.updated_at > stale_before:
            return None
        self.record.reconciliation_owner = owner
        self.record.reconciliation_lease_until = lease_until
        self.record.reconciliation_attempts += 1
        return self.record

    def mark_reconciled_unknown(self, record, *, error_reason: str | None, owner: str):
        assert record.reconciliation_owner == owner
        record.status = SettlementStatus.UNKNOWN
        record.error_reason = error_reason
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None

    def mark_reconciled_settled(
        self, record, *, transaction: str | None, payer: str | None, owner: str
    ):
        assert record.reconciliation_owner == owner
        record.status = SettlementStatus.SETTLED
        record.transaction = transaction
        record.payer = payer
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None

    def mark_reconciled_failed(self, record, *, error_reason: str | None, owner: str):
        assert record.reconciliation_owner == owner
        record.status = SettlementStatus.SETTLE_FAILED
        record.error_reason = error_reason
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None


class _MultiReconciliationStore:
    def __init__(self, records: list[SettlementRecord]):
        self.records = records

    def list_reconciliation_candidates(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before,
        limit: int,
    ):
        return [
            record
            for record in self.records
            if record.status in statuses and record.updated_at <= stale_before
        ][:limit]


def _settle_in_progress_record(updated_at) -> SettlementRecord:
    return SettlementRecord(
        record_id=1,
        seller_account_id="seller-a",
        fingerprint="fp-stale",
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        status=SettlementStatus.SETTLE_IN_PROGRESS,
        trace_id="tr-stale",
        created_at=updated_at,
        updated_at=updated_at,
        raw_requirements={"scheme": "exact", "network": "eip155:5042002"},
    )


def _submitted_record(updated_at, *, record_id: int, fingerprint: str) -> SettlementRecord:
    return SettlementRecord(
        record_id=record_id,
        seller_account_id="seller-a",
        fingerprint=fingerprint,
        provider="exact_evm",
        scheme="exact",
        network="eip155:5042002",
        status=SettlementStatus.SUBMITTED,
        trace_id=f"tr-{fingerprint}",
        transaction=f"0x{record_id:064x}",
        created_at=updated_at,
        updated_at=updated_at,
        raw_requirements={"scheme": "exact", "network": "eip155:5042002"},
    )


def _runtime_config(config=None) -> HostedExactRuntimeConfig:
    exact_config = config or ExactFacilitatorConfig(
        private_key="0xhostedsigner",
        rpc_url="https://rpc.example",
        networks=("eip155:5042002",),
    )
    network = getattr(exact_config, "networks", ("eip155:5042002",))[0]
    signer_guard = HostedSignerGuard(
        HostedSignerConfig(
            signer_id="signer-exact-alpha",
            provider="exact_evm",
            network=network,
            environment="test",
        )
    )
    return HostedExactRuntimeConfig(
        exact_config=exact_config,
        network_configs=(exact_config,),
        signer_guard=signer_guard,
    )


@pytest.mark.asyncio
async def test_reconciler_marks_stale_settle_in_progress_unknown_when_provider_is_pending():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(
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

    assert stats.scanned == 1
    assert stats.claimed == 1
    assert stats.marked_unknown == 1
    assert stats.pending == 0
    assert provider.reconcile_calls == 1
    assert provider.settle_calls == 0
    assert record.status == SettlementStatus.UNKNOWN
    assert record.error_reason == "stale_settle_in_progress"


@pytest.mark.asyncio
async def test_reconciler_marks_stale_settle_in_progress_unknown_when_provider_missing():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.scanned == 1
    assert stats.claimed == 1
    assert stats.marked_unknown == 1
    assert stats.pending == 0
    assert record.status == SettlementStatus.UNKNOWN
    assert record.error_reason == "reconciliation_provider_not_registered"


@pytest.mark.asyncio
async def test_reconciler_does_not_scan_settle_in_progress_before_its_own_threshold():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(
        ReconciliationOutcome(status=ReconciliationStatus.PENDING),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=5),
        stale_submitted_after=timedelta(minutes=1),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.scanned == 0
    assert stats.claimed == 0
    assert provider.reconcile_calls == 0
    assert provider.settle_calls == 0
    assert record.status == SettlementStatus.SETTLE_IN_PROGRESS


@pytest.mark.asyncio
async def test_reconciler_marks_stale_settle_in_progress_unknown_on_provider_exception():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(RuntimeError("rpc unavailable"))
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.errors == 1
    assert stats.marked_unknown == 1
    assert stats.pending == 0
    assert record.status == SettlementStatus.UNKNOWN
    assert record.error_reason == "provider_reconciliation_exception"


@pytest.mark.asyncio
async def test_reconciler_settles_stale_settle_in_progress_with_provider_proof():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.SETTLED,
            transaction="0xsettled",
            payer="0xpayer",
        ),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.settled == 1
    assert stats.marked_unknown == 0
    assert provider.reconcile_calls == 1
    assert provider.settle_calls == 0
    assert record.status == SettlementStatus.SETTLED
    assert record.transaction == "0xsettled"


@pytest.mark.asyncio
async def test_reconciler_does_not_terminal_fail_stale_settle_in_progress():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.FAILED_FINAL,
            error_reason="provider_claimed_failed_without_proof",
        ),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.failed == 0
    assert stats.marked_unknown == 1
    assert record.status == SettlementStatus.UNKNOWN
    assert record.error_reason == "provider_claimed_failed_without_proof"


@pytest.mark.asyncio
async def test_reconciler_marks_stale_settle_in_progress_unknown_from_provider_unknown():
    now = utcnow()
    record = _settle_in_progress_record(now - timedelta(minutes=3))
    store = _ReconciliationStore(record)
    provider = _ReconcilingProvider(
        ReconciliationOutcome(
            status=ReconciliationStatus.UNKNOWN,
            error_reason="provider_cannot_prove_outcome",
        ),
    )
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([provider]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        clock=lambda: now,
    )

    stats = await reconciler.run_once()

    assert stats.marked_unknown == 1
    assert stats.failed == 0
    assert provider.reconcile_calls == 1
    assert provider.settle_calls == 0
    assert record.status == SettlementStatus.UNKNOWN
    assert record.error_reason == "provider_cannot_prove_outcome"


def test_reconciler_scan_does_not_starve_stale_settle_in_progress_records():
    now = utcnow()
    records = [
        _submitted_record(
            now - timedelta(minutes=3),
            record_id=index + 10,
            fingerprint=f"fp-submitted-{index}",
        )
        for index in range(5)
    ]
    in_progress = _settle_in_progress_record(now - timedelta(minutes=3))
    records.append(in_progress)
    store = _MultiReconciliationStore(records)
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter([]),
        store=store,
        owner="worker-a",
        stale_in_progress_after=timedelta(minutes=2),
        stale_submitted_after=timedelta(minutes=2),
        limit=2,
        clock=lambda: now,
    )

    candidates = reconciler._scan_candidates(now)

    assert in_progress in candidates
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_hosted_reconciliation_runner_aggregates_bounded_runs():
    sleep_calls = []
    first = ReconciliationRunStats(scanned=2, claimed=1, settled=1)
    second = ReconciliationRunStats(scanned=3, claimed=2, pending=2)
    reconciler = _FakeReconciler(first, second)

    async def sleep(seconds):
        sleep_calls.append(seconds)

    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=0.25,
        max_runs=2,
        sleep=sleep,
    )

    stats = await runner.run()

    assert stats.runs == 2
    assert stats.failed_runs == 0
    assert stats.aggregate.scanned == 5
    assert stats.aggregate.claimed == 3
    assert stats.aggregate.settled == 1
    assert stats.aggregate.pending == 2
    assert sleep_calls == [0.25]


@pytest.mark.asyncio
async def test_hosted_reconciliation_runner_once_exits_after_run_level_failure():
    sleep_calls = []
    reconciler = _FakeReconciler(RuntimeError("database unavailable"))

    async def sleep(seconds):
        sleep_calls.append(seconds)

    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        error_interval_seconds=9,
        max_runs=1,
        sleep=sleep,
    )

    stats = await runner.run()

    assert stats.runs == 0
    assert stats.failed_runs == 1
    assert stats.last_error == "database unavailable"
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_hosted_reconciliation_runner_stops_without_sleep_after_mixed_attempt_budget():
    sleep_calls = []
    reconciler = _FakeReconciler(
        RuntimeError("temporary outage"), ReconciliationRunStats(settled=1)
    )

    async def sleep(seconds):
        sleep_calls.append(seconds)

    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=5,
        error_interval_seconds=0,
        max_runs=2,
        sleep=sleep,
    )

    stats = await runner.run()

    assert stats.runs == 1
    assert stats.failed_runs == 1
    assert stats.aggregate.settled == 1
    assert sleep_calls == [0]


@pytest.mark.asyncio
async def test_hosted_reconciliation_runner_can_stop_from_iteration_callback():
    reconciler = _FakeReconciler(
        ReconciliationRunStats(scanned=1),
        ReconciliationRunStats(scanned=1),
    )

    def stop_after_first(_stats):
        runner.stop()

    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=0,
        on_iteration=stop_after_first,
    )

    stats = await runner.run()

    assert stats.runs == 1
    assert reconciler.calls == 1


@pytest.mark.asyncio
async def test_hosted_reconciliation_runner_can_stop_during_positive_sleep():
    sleep_started = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    reconciler = _FakeReconciler(ReconciliationRunStats(scanned=1))

    async def sleep(_seconds):
        sleep_started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            sleep_cancelled.set()
            raise

    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=30,
        sleep=sleep,
    )

    task = asyncio.create_task(runner.run())
    await sleep_started.wait()
    runner.stop()
    stats = await task

    assert stats.runs == 1
    assert sleep_cancelled.is_set()


def test_reconciliation_owner_from_env_uses_explicit_owner(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_RECONCILER_OWNER", "worker-explicit")
    monkeypatch.setenv("HOSTNAME", "ignored")

    assert reconciliation_owner_from_env() == "worker-explicit"


def test_reconciliation_owner_from_env_uses_hostname(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_RECONCILER_OWNER", raising=False)
    monkeypatch.setenv("HOSTNAME", "pod-7")

    assert reconciliation_owner_from_env() == "hosted-reconciler-pod-7"


def _hosted_required_runtime_env(monkeypatch):
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://svc:secret@pg.internal/omniclaw?sslmode=require",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN",
        "postgresql://svc:secret@pg.internal/omniclaw_control?sslmode=require",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "postgresql://svc:secret@pg.internal/omniclaw_control?sslmode=require",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", "rediss://redis.internal:6379/0")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_ISSUER", "https://idp.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_AUDIENCE", "omniclaw-ops")
    monkeypatch.setenv("OMNICLAW_HOSTED_OIDC_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_API_URL", "https://openfga.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_STORE_ID", "store-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID", "model-id")
    monkeypatch.setenv("OMNICLAW_HOSTED_OTEL_REQUIRED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_JSON_LOGS", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.example.test")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL", "https://otel.example.test/health"
    )


def test_load_hosted_exact_runtime_config_supports_multiple_networks_with_one_signer(
    monkeypatch,
):
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://fallback-rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002,eip155:84532")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002",
        "https://arc-rpc.example",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_84532",
        "https://base-rpc.example",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "1000000000000000")

    runtime = load_hosted_exact_runtime_config_from_env()

    assert [config.networks for config in runtime.network_configs] == [
        ("eip155:5042002",),
        ("eip155:84532",),
    ]
    assert [config.rpc_url for config in runtime.network_configs] == [
        "https://arc-rpc.example",
        "https://base-rpc.example",
    ]
    assert {config.private_key for config in runtime.network_configs} == {"0xshared"}
    assert runtime.signer_guard.config.signer_id == "hosted-exact-alpha"
    assert runtime.signer_guard.config.network == "evm"
    assert runtime.signer_guard.config.min_native_balance_wei == 1000000000000000


def test_load_hosted_exact_runtime_config_rejects_duplicate_networks(monkeypatch):
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002,eip155:5042002")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")

    with pytest.raises(RuntimeError, match="Duplicate hosted exact EVM network"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_invalid_network(monkeypatch):
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "base-sepolia")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")

    with pytest.raises(RuntimeError, match="Invalid hosted exact EVM network"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_requires_gas_floor_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "staging")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_incomplete_hosted_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="Hosted runtime configuration is missing"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_local_compose_secrets_in_hosted_env(
    monkeypatch,
):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://omniclaw:omniclaw@postgres:5432/omniclaw",
    )
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="local Compose credentials"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_requires_postgres_tls_in_hosted_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://svc:secret@pg.internal/omniclaw",
    )
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="sslmode"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_requires_redis_tls_in_hosted_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="rediss"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_default_api_key(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_DEFAULT_API_KEY", "omck_forbidden_default")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_DEFAULT_API_KEY"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_requires_scoped_rpc_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://fallback-rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002")
    monkeypatch.delenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", raising=False)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_unsafe_scoped_rpc_in_hosted_mode(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://fallback-rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002",
        "http://user:pass@localhost:8545",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_does_not_require_circle_credentials(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_84532", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")

    config = load_hosted_exact_runtime_config_from_env()

    assert config.exact_config.rpc_url == "https://rpc.example"


def test_load_hosted_exact_runtime_config_rejects_local_circle_base_url(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL", "http://localhost:8080")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL"):
        load_hosted_exact_runtime_config_from_env()


def test_load_hosted_exact_runtime_config_rejects_unpinned_openfga_model(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_HOSTED_OPENFGA_ALLOW_DYNAMIC_MODEL_ID", "true")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")

    with pytest.raises(RuntimeError, match="must pin"):
        load_hosted_exact_runtime_config_from_env()


def test_hosted_exact_provider_rejects_invalid_concurrency_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS", "0")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS"):
        _hosted_exact_max_concurrent_settlements()


def test_hosted_exact_app_entrypoint_rejects_invalid_concurrency_env(monkeypatch):
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_MODE", "production")
    _hosted_required_runtime_env(monkeypatch)
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_NETWORK_PROFILE", "ARC-TESTNET")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002")
    monkeypatch.setenv("OMNICLAW_X402_FACILITATOR_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY", "0xshared")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "100")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS", "0")

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS"):
        create_hosted_exact_facilitator_app_from_env()


def test_create_hosted_exact_reconciler_from_env_wires_store_and_provider(monkeypatch):
    class _Store:
        initialized = False

        def initialize(self):
            self.initialized = True

    class _ExactApp:
        state = type("_State", (), {"omniclaw_exact_facilitator": object()})

    store = _Store()
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_exact_facilitator_app",
        lambda _config: _ExactApp(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: store,
    )

    reconciler, returned_store = create_hosted_exact_reconciler_from_env(
        owner="worker-a",
        lease_seconds=11,
        stale_in_progress_after=timedelta(seconds=5),
        stale_submitted_after=timedelta(seconds=1),
        stale_unknown_after=timedelta(seconds=2),
        manual_review_after=timedelta(seconds=3),
        limit=4,
    )

    assert returned_store is store
    assert store.initialized is True
    assert reconciler._owner == "worker-a"
    assert reconciler._lease_seconds == 11
    assert reconciler._stale_in_progress_after == timedelta(seconds=5)
    assert reconciler._limit == 4


def test_create_hosted_exact_reconciler_from_env_closes_store_on_initialize_failure(monkeypatch):
    class _Store:
        closed = False

        def initialize(self):
            raise RuntimeError("migration failed")

        def close(self):
            self.closed = True

    class _ExactApp:
        state = type("_State", (), {"omniclaw_exact_facilitator": object()})

    store = _Store()
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_exact_facilitator_app",
        lambda _config: _ExactApp(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: store,
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        create_hosted_exact_reconciler_from_env(owner="worker-a")

    assert store.closed is True


def test_create_hosted_exact_facilitator_app_from_env_wires_postgres_resources(monkeypatch):
    config = object()
    store = object()
    control_plane_state = object()
    resolver = object()
    project = SellerAccountConfig(seller_account_id="seller-a", api_keys=("secret-key",))
    captured = {}

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        lambda: _runtime_config(config),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: control_plane_state,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: resolver,
    )

    def create_app(config_arg, **kwargs):
        captured["config"] = config_arg
        captured.update(kwargs)
        return "hosted-app"

    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        create_app,
    )

    app = create_hosted_exact_facilitator_app_from_env(seller_accounts=[project])

    assert app == "hosted-app"
    assert captured["config"] is config
    assert captured["seller_accounts"] == [project]
    assert captured["store_factory"]() is store
    assert captured["control_plane_state"] is control_plane_state
    assert captured["rate_limiter"] is None
    assert captured["resolver"] is resolver


def test_create_hosted_exact_facilitator_app_from_env_bootstraps_projects_on_initialize(
    monkeypatch,
):
    config = object()
    project = SellerAccountConfig(seller_account_id="seller-a", api_keys=("secret-key",))
    captured = {}

    class _Resolver:
        def __init__(self):
            self.initialized = False
            self.bootstrapped = None

        def initialize(self):
            self.initialized = True

        def bootstrap_seller_accounts(self, projects):
            self.bootstrapped = projects

    resolver = _Resolver()

    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        lambda: _runtime_config(config),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: resolver,
    )

    def create_app(config_arg, **kwargs):
        captured["config"] = config_arg
        captured.update(kwargs)
        return "hosted-app"

    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        create_app,
    )

    app = create_hosted_exact_facilitator_app_from_env(
        seller_accounts=[project],
        initialize_storage=True,
    )

    assert app == "hosted-app"
    assert resolver.initialized is True
    assert resolver.bootstrapped == [project]
    assert captured["resolver"] is resolver


def test_create_hosted_exact_facilitator_app_from_env_can_run_without_postgres(monkeypatch):
    captured = {}

    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **kwargs: captured.update(kwargs) or "hosted-app",
    )

    app = create_hosted_exact_facilitator_app_from_env()

    assert app == "hosted-app"
    assert captured["store_factory"] is None
    assert captured["rate_limiter"] is None
    assert captured["control_plane_state"] is None
    assert captured["resolver"] is None


def test_create_hosted_exact_facilitator_app_from_env_wires_postgres_seller_account_resolver(
    monkeypatch,
):
    resolver = object()
    captured = {}

    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: resolver,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **kwargs: captured.update(kwargs) or "hosted-app",
    )

    app = create_hosted_exact_facilitator_app_from_env()

    assert app == "hosted-app"
    assert captured["resolver"] is resolver


def test_create_hosted_exact_facilitator_app_from_env_uses_hosted_dsn_for_projects(
    monkeypatch,
):
    resolver = object()
    captured = {}

    monkeypatch.delenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_FROM_POSTGRES", raising=False)
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://hosted")
    monkeypatch.delenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: resolver,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **kwargs: captured.update(kwargs) or "hosted-app",
    )

    app = create_hosted_exact_facilitator_app_from_env()

    assert app == "hosted-app"
    assert captured["resolver"] is resolver


def test_create_hosted_exact_facilitator_app_from_env_wires_redis_rate_limiter(monkeypatch):
    captured = {}

    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", "redis://example.invalid/0")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **kwargs: captured.update(kwargs) or "hosted-app",
    )

    app = create_hosted_exact_facilitator_app_from_env()

    assert app == "hosted-app"
    assert captured["rate_limiter"].hosted_safe is True


def test_create_hosted_exact_facilitator_app_from_env_preflight_initializes_short_lived_store(
    monkeypatch,
):
    class _Store:
        def __init__(self):
            self.initialize_calls = 0
            self.close_calls = 0

        def initialize(self):
            self.initialize_calls += 1

        def close(self):
            self.close_calls += 1

    class _ControlPlaneState:
        def __init__(self):
            self.initialize_calls = 0
            self.close_calls = 0

        def initialize(self):
            self.initialize_calls += 1

        def close(self):
            self.close_calls += 1

    class _ProjectResolver:
        def __init__(self):
            self.initialize_calls = 0
            self.close_calls = 0

        def initialize(self):
            self.initialize_calls += 1

        def close(self):
            self.close_calls += 1

    stores: list[_Store] = []
    control_plane_state = _ControlPlaneState()
    project_resolver = _ProjectResolver()
    captured = {}

    def store_factory():
        store = _Store()
        stores.append(store)
        return store

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        store_factory,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: control_plane_state,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: object(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: project_resolver,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **kwargs: captured.update(kwargs) or "hosted-app",
    )

    app = create_hosted_exact_facilitator_app_from_env(initialize_storage=True)

    assert app == "hosted-app"
    assert len(stores) == 1
    assert stores[0].initialize_calls == 1
    assert stores[0].close_calls == 1
    assert control_plane_state.initialize_calls == 1
    assert control_plane_state.close_calls == 0
    assert project_resolver.initialize_calls == 1
    assert project_resolver.close_calls == 0
    assert captured["store_factory"] is store_factory
    assert captured["resolver"] is project_resolver


def test_create_hosted_exact_facilitator_app_from_env_closes_preflight_resources_when_app_build_fails(
    monkeypatch,
):
    class _Store:
        def initialize(self):
            return None

        def close(self):
            return None

    class _ControlPlaneState:
        def __init__(self):
            self.close_calls = 0

        def initialize(self):
            return None

        def close(self):
            self.close_calls += 1

    class _ProjectResolver:
        def __init__(self):
            self.close_calls = 0

        def initialize(self):
            return None

        def close(self):
            self.close_calls += 1

    control_plane_state = _ControlPlaneState()
    project_resolver = _ProjectResolver()

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: _Store(),
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: control_plane_state,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: project_resolver,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.create_hosted_exact_facilitator_app",
        lambda _config, **_kwargs: (_ for _ in ()).throw(RuntimeError("app build failed")),
    )

    with pytest.raises(RuntimeError, match="app build failed"):
        create_hosted_exact_facilitator_app_from_env(initialize_storage=True)

    assert control_plane_state.close_calls == 1
    assert project_resolver.close_calls == 1


def test_create_hosted_exact_facilitator_app_from_env_closes_initialized_storage_on_failure(
    monkeypatch,
):
    class _Store:
        initialized = False
        closed = False

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

    class _ControlPlaneState:
        closed = False

        def initialize(self):
            raise RuntimeError("control migration failed")

        def close(self):
            self.closed = True

    store = _Store()
    control_plane_state = _ControlPlaneState()

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: control_plane_state,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: object(),
    )

    with pytest.raises(RuntimeError, match="control migration failed"):
        create_hosted_exact_facilitator_app_from_env(initialize_storage=True)

    assert store.initialized is True
    assert store.closed is True
    assert control_plane_state.closed is True


def test_create_hosted_exact_facilitator_app_from_env_closes_on_resolver_preflight_failure(
    monkeypatch,
):
    class _Store:
        initialized = False
        closed = False

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

    class _ControlPlaneState:
        initialized = False
        closed = False

        def initialize(self):
            self.initialized = True

        def close(self):
            self.closed = True

    class _ProjectResolver:
        closed = False

        def initialize(self):
            raise RuntimeError("project migration failed")

        def close(self):
            self.closed = True

    store = _Store()
    control_plane_state = _ControlPlaneState()
    project_resolver = _ProjectResolver()

    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "postgresql://settlement")
    monkeypatch.setenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "postgresql://control")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "postgresql://seller-accounts"
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.load_hosted_exact_runtime_config_from_env",
        _runtime_config,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_store_from_env",
        lambda: store,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_control_plane_state_from_env",
        lambda: control_plane_state,
    )
    monkeypatch.setattr(
        "hosted_facilitator.hosted.runner.postgres_seller_account_resolver_from_env",
        lambda: project_resolver,
    )

    with pytest.raises(RuntimeError, match="project migration failed"):
        create_hosted_exact_facilitator_app_from_env(initialize_storage=True)

    assert store.initialized is True
    assert store.closed is True
    assert control_plane_state.initialized is True
    assert control_plane_state.closed is True
    assert project_resolver.closed is True
