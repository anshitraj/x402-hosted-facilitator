from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from hosted_facilitator.exact import (
    ExactFacilitatorConfig,
    create_exact_facilitator_app,
    load_exact_facilitator_config_from_env,
)
from hosted_facilitator.hosted.app import (
    create_hosted_exact_facilitator_app,
    create_hosted_facilitator_app,
)
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.limits import RedisFixedWindowRateLimiter
from hosted_facilitator.hosted.postgres import (
    PostgresSettlementStorePool,
    postgres_control_plane_state_from_env,
    postgres_seller_account_resolver_from_env,
    postgres_store_from_env,
)
from hosted_facilitator.hosted.providers.circle_gateway import (
    create_hosted_circle_gateway_provider_from_env,
)
from hosted_facilitator.hosted.providers.exact_evm import HostedExactProvider
from hosted_facilitator.hosted.reconciliation import (
    HostedSettlementReconciler,
    ReconciliationRunStats,
)
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.signers import (
    HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV,
    HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV_ENV,
    HostedSignerGuard,
    load_hosted_exact_signer_env,
    with_signer_private_key,
)
from hosted_facilitator.hosted.storage import utcnow
from hosted_facilitator.hosted.tenancy import SellerAccountConfig


@dataclass
class ReconciliationLoopStats:
    runs: int = 0
    failed_runs: int = 0
    aggregate: ReconciliationRunStats = field(default_factory=ReconciliationRunStats)
    last_run: ReconciliationRunStats | None = None
    last_error: str | None = None
    started_at: datetime | None = None
    stopped_at: datetime | None = None


@dataclass(frozen=True)
class HostedExactRuntimeConfig:
    exact_config: ExactFacilitatorConfig
    network_configs: tuple[ExactFacilitatorConfig, ...]
    signer_guard: HostedSignerGuard


HOSTED_EXACT_ENABLED_ENV = "OMNICLAW_HOSTED_EXACT_ENABLED"


class HostedReconciliationRunner:
    def __init__(
        self,
        *,
        reconciler: HostedSettlementReconciler,
        interval_seconds: float = 5.0,
        error_interval_seconds: float = 30.0,
        max_runs: int | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_iteration: Callable[[ReconciliationRunStats], Any] | None = None,
    ):
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be non-negative")
        if error_interval_seconds < 0:
            raise ValueError("error_interval_seconds must be non-negative")
        if max_runs is not None and max_runs <= 0:
            raise ValueError("max_runs must be positive")
        self._reconciler = reconciler
        self._interval_seconds = interval_seconds
        self._error_interval_seconds = error_interval_seconds
        self._max_runs = max_runs
        self._clock = clock
        self._sleep = sleep
        self._on_iteration = on_iteration
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> ReconciliationLoopStats:
        stats = ReconciliationLoopStats(started_at=self._clock())
        try:
            while not self._stop_event.is_set():
                if self._max_runs is not None and stats.runs + stats.failed_runs >= self._max_runs:
                    break
                try:
                    run_stats = await self._reconciler.run_once()
                except Exception as exc:
                    stats.failed_runs += 1
                    stats.last_error = str(exc)
                    if (
                        self._max_runs is not None
                        and stats.runs + stats.failed_runs >= self._max_runs
                    ):
                        break
                    if not await self._sleep_or_stop(self._error_interval_seconds):
                        break
                    continue
                stats.runs += 1
                stats.last_run = run_stats
                stats.last_error = None
                _add_stats(stats.aggregate, run_stats)
                if self._on_iteration is not None:
                    callback_result = self._on_iteration(run_stats)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                if self._max_runs is not None and stats.runs + stats.failed_runs >= self._max_runs:
                    break
                if not await self._sleep_or_stop(self._interval_seconds):
                    break
        finally:
            stats.stopped_at = self._clock()
        return stats

    async def _sleep_or_stop(self, seconds: float) -> bool:
        if self._stop_event.is_set():
            return False
        if seconds <= 0:
            await self._sleep(0)
            return not self._stop_event.is_set()
        sleep_task = asyncio.create_task(self._sleep(seconds))
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            {sleep_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            await task
        return not self._stop_event.is_set()


def create_hosted_exact_reconciler_from_env(
    *,
    owner: str,
    lease_seconds: int = 60,
    stale_in_progress_after: timedelta = timedelta(minutes=2),
    stale_submitted_after: timedelta = timedelta(minutes=2),
    stale_unknown_after: timedelta = timedelta(minutes=5),
    manual_review_after: timedelta = timedelta(minutes=30),
    limit: int = 100,
) -> tuple[HostedSettlementReconciler, Any]:
    runtime_config = load_hosted_exact_runtime_config_from_env()
    provider, _exact_facilitators = _hosted_exact_provider_from_runtime_config(runtime_config)
    providers = [provider]
    circle_provider = create_hosted_circle_gateway_provider_from_env()
    if circle_provider is not None:
        providers.append(circle_provider)
    store = postgres_store_from_env()
    try:
        store.initialize()
    except Exception:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        raise
    reconciler = HostedSettlementReconciler(
        router=ProviderRouter(providers),
        store=store,
        owner=owner,
        lease_seconds=lease_seconds,
        stale_in_progress_after=stale_in_progress_after,
        stale_submitted_after=stale_submitted_after,
        stale_unknown_after=stale_unknown_after,
        manual_review_after=manual_review_after,
        limit=limit,
    )
    return reconciler, store


def create_hosted_facilitator_app_from_env(
    *,
    seller_accounts: list[SellerAccountConfig] | None = None,
    initialize_storage: bool = False,
):
    _assert_hosted_runtime_env_safe()
    providers: list[Any] = []
    exact_runtime_config: HostedExactRuntimeConfig | None = None
    exact_facilitators: dict[str, Any] = {}
    if _hosted_exact_enabled_from_env():
        exact_runtime_config = load_hosted_exact_runtime_config_from_env()
        exact_provider, exact_facilitators = _hosted_exact_provider_from_runtime_config(
            exact_runtime_config
        )
        providers.append(exact_provider)
    providers.extend(_extra_hosted_providers_from_env())
    if not providers:
        raise RuntimeError("At least one hosted facilitator provider must be enabled")

    store_factory = _postgres_store_factory_from_env_if_configured()
    control_plane_state = _postgres_control_plane_state_from_env_if_configured()
    rate_limiter = _rate_limiter_from_env_if_configured()
    resolver = _postgres_seller_account_resolver_from_env_if_configured()
    if initialize_storage:
        _preflight_storage(store_factory, control_plane_state, resolver)
        _bootstrap_seller_accounts(resolver, seller_accounts)
    try:
        engine = HostedFacilitatorEngine(router=ProviderRouter(providers))
        app = create_hosted_facilitator_app(
            engine=engine,
            resolver=resolver,
            store_factory=store_factory,
            rate_limiter=rate_limiter,
            control_plane_state=control_plane_state,
        )
        app.state.omniclaw_hosted_providers = {provider.name: provider for provider in providers}
        if exact_runtime_config is not None:
            app.state.omniclaw_exact_facilitator_config = {
                "host": exact_runtime_config.exact_config.host,
                "port": exact_runtime_config.exact_config.port,
                "rpc_url": exact_runtime_config.exact_config.rpc_url,
                "networks": exact_runtime_config.exact_config.networks,
                "network_profile": exact_runtime_config.exact_config.network_profile,
                "title": exact_runtime_config.exact_config.title,
            }
            app.state.omniclaw_exact_facilitator = next(iter(exact_facilitators.values()))
            app.state.omniclaw_exact_facilitators_by_network = exact_facilitators
        return app
    except Exception:
        _close_resource(resolver)
        _close_resource(control_plane_state)
        raise


def create_hosted_exact_facilitator_app_from_env(
    *,
    seller_accounts: list[SellerAccountConfig] | None = None,
    initialize_storage: bool = False,
):
    runtime_config = load_hosted_exact_runtime_config_from_env()
    provider_timeout_seconds = _hosted_exact_provider_timeout_seconds()
    max_concurrent_settlements = _hosted_exact_max_concurrent_settlements()
    store_factory = _postgres_store_factory_from_env_if_configured()
    control_plane_state = _postgres_control_plane_state_from_env_if_configured()
    rate_limiter = _rate_limiter_from_env_if_configured()
    resolver = _postgres_seller_account_resolver_from_env_if_configured()
    if initialize_storage:
        _preflight_storage(store_factory, control_plane_state, resolver)
        _bootstrap_seller_accounts(resolver, seller_accounts)
    try:
        extra_providers = _extra_hosted_providers_from_env()
        return create_hosted_exact_facilitator_app(
            runtime_config.exact_config,
            network_configs=runtime_config.network_configs,
            seller_accounts=seller_accounts,
            resolver=resolver,
            store_factory=store_factory,
            rate_limiter=rate_limiter,
            control_plane_state=control_plane_state,
            signer_guard=runtime_config.signer_guard,
            extra_providers=extra_providers,
            provider_timeout_seconds=provider_timeout_seconds,
            max_concurrent_settlements=max_concurrent_settlements,
        )
    except Exception:
        _close_resource(resolver)
        _close_resource(control_plane_state)
        raise


def _extra_hosted_providers_from_env() -> tuple[Any, ...]:
    providers = []
    circle_provider = create_hosted_circle_gateway_provider_from_env()
    if circle_provider is not None:
        providers.append(circle_provider)
    return tuple(providers)


def _hosted_exact_enabled_from_env() -> bool:
    raw = os.getenv(HOSTED_EXACT_ENABLED_ENV, "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _hosted_exact_provider_from_runtime_config(
    runtime_config: HostedExactRuntimeConfig,
) -> tuple[HostedExactProvider, dict[str, Any]]:
    exact_facilitators = {}
    for network_config in runtime_config.network_configs:
        exact_app = create_exact_facilitator_app(network_config)
        for network in network_config.networks:
            exact_facilitators[network] = exact_app.state.omniclaw_exact_facilitator
    return (
        HostedExactProvider(
            exact_facilitators,
            signer_guard=runtime_config.signer_guard,
            timeout_seconds=_hosted_exact_provider_timeout_seconds(),
            max_concurrent_settlements=_hosted_exact_max_concurrent_settlements(),
        ),
        exact_facilitators,
    )


def load_hosted_exact_runtime_config_from_env() -> HostedExactRuntimeConfig:
    _assert_hosted_runtime_env_safe()
    private_key_env_var = (
        os.environ.get(HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV_ENV, "").strip()
        or HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV
    )
    exact_config = load_exact_facilitator_config_from_env(
        private_key_env_names=(private_key_env_var,),
    )
    networks = _hosted_exact_networks(exact_config)
    signer_env = load_hosted_exact_signer_env(default_network="evm")
    _assert_hosted_exact_signer_env_safe(signer_env)
    exact_config = with_signer_private_key(exact_config, signer_env.private_key)
    network_configs: list[ExactFacilitatorConfig] = []
    for network in networks:
        suffix = _network_env_suffix(network)
        network_configs.append(
            replace(
                exact_config,
                rpc_url=_hosted_exact_rpc_url(
                    network, suffix, exact_config, multi_network=len(networks) > 1
                ),
                networks=(network,),
            )
        )
    primary_config = network_configs[0]
    return HostedExactRuntimeConfig(
        exact_config=primary_config,
        network_configs=tuple(network_configs),
        signer_guard=HostedSignerGuard(signer_env.signer_config),
    )


def _hosted_exact_networks(exact_config: ExactFacilitatorConfig) -> tuple[str, ...]:
    raw = os.environ.get("OMNICLAW_HOSTED_EXACT_NETWORKS", "").strip()
    if raw:
        networks = tuple(network.strip() for network in raw.split(",") if network.strip())
    else:
        networks = exact_config.networks
    if not networks:
        raise RuntimeError("At least one hosted exact EVM network must be configured")
    seen: set[str] = set()
    for network in networks:
        if not _is_evm_network(network):
            raise RuntimeError(f"Invalid hosted exact EVM network: {network}")
        if network in seen:
            raise RuntimeError(f"Duplicate hosted exact EVM network: {network}")
        seen.add(network)
    return networks


def _assert_hosted_runtime_env_safe() -> None:
    mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    if mode in {"", "local", "test"}:
        return
    required = (
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN",
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        "OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL",
        "OMNICLAW_HOSTED_OIDC_ISSUER",
        "OMNICLAW_HOSTED_OIDC_AUDIENCE",
        "OMNICLAW_HOSTED_OIDC_JWKS_URL",
        "OMNICLAW_HOSTED_OPENFGA_API_URL",
        "OMNICLAW_HOSTED_OPENFGA_STORE_ID",
        "OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(f"Hosted runtime configuration is missing: {', '.join(missing)}")
    if os.environ.get("OMNICLAW_HOSTED_OPENFGA_ALLOW_DYNAMIC_MODEL_ID", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("Hosted runtime must pin OMNICLAW_HOSTED_OPENFGA_AUTHORIZATION_MODEL_ID")
    if os.environ.get("OMNICLAW_HOSTED_OTEL_REQUIRED", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("Hosted runtime requires OMNICLAW_HOSTED_OTEL_REQUIRED=true")
    if os.environ.get("OMNICLAW_HOSTED_JSON_LOGS", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise RuntimeError("Hosted runtime requires OMNICLAW_HOSTED_JSON_LOGS=true")
    _reject_local_secret_url(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        disallowed_markers=("omniclaw:omniclaw@", "@postgres:5432/omniclaw"),
    )
    _reject_local_secret_url(
        "OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN",
        disallowed_markers=("omniclaw:omniclaw@", "@postgres:5432/omniclaw"),
    )
    _reject_local_secret_url(
        "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN",
        disallowed_markers=("omniclaw:omniclaw@", "@postgres:5432/omniclaw"),
    )
    _reject_local_secret_url(
        "OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL",
        disallowed_markers=("redis://redis:6379/0",),
    )
    _assert_postgres_dsn_tls("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN")
    _assert_postgres_dsn_tls("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN")
    _assert_postgres_dsn_tls("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN")
    _assert_control_plane_and_seller_accounts_share_database()
    _assert_redis_url_tls("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL")
    _reject_hosted_unsafe_secrets()
    for name in (
        "OMNICLAW_HOSTED_OIDC_ISSUER",
        "OMNICLAW_HOSTED_OIDC_JWKS_URL",
        "OMNICLAW_HOSTED_OPENFGA_API_URL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL",
    ):
        _assert_hosted_url_safe(name)
    if (
        os.environ.get("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and os.environ.get("OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL", "").strip()
    ):
        _assert_hosted_url_safe("OMNICLAW_HOSTED_CIRCLE_GATEWAY_BASE_URL")


def _reject_local_secret_url(name: str, *, disallowed_markers: tuple[str, ...]) -> None:
    value = os.environ.get(name, "").strip().lower()
    if any(marker in value for marker in disallowed_markers):
        raise RuntimeError(f"{name} uses local Compose credentials or endpoints")
    _assert_hosted_url_safe(name)


def _assert_hosted_url_safe(name: str) -> None:
    value = os.environ.get(name, "").strip()
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"{name} must be an absolute URL")
    if (parsed.username or parsed.password) and not (
        name.endswith("_DSN") or name.endswith("_REDIS_URL")
    ):
        raise RuntimeError(f"{name} must not contain credentials")
    if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise RuntimeError(f"{name} must not point at a local endpoint in hosted mode")
    if parsed.scheme == "http":
        raise RuntimeError(f"{name} must use TLS in hosted mode")


def _assert_postgres_dsn_tls(name: str) -> None:
    value = os.environ.get(name, "").strip()
    parsed = urlparse(value)
    query = {key.lower(): values[-1].lower() for key, values in parse_qs(parsed.query).items()}
    sslmode = query.get("sslmode")
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise RuntimeError(
            f"{name} must set sslmode=require, verify-ca, or verify-full in hosted mode"
        )


def _assert_control_plane_and_seller_accounts_share_database() -> None:
    control_dsn = (
        os.environ.get("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN", "").strip()
        or os.environ.get("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    seller_dsn = (
        os.environ.get("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN", "").strip()
        or os.environ.get("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
    if _postgres_database_identity(control_dsn) != _postgres_database_identity(seller_dsn):
        raise RuntimeError(
            "OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN and "
            "OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN must point at the same "
            "Postgres database so seller policy changes and audit events are atomic"
        )


def _postgres_database_identity(
    dsn: str,
) -> tuple[str, str, int, str, str, tuple[tuple[str, tuple[str, ...]], ...]]:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("Hosted Postgres DSNs must use postgresql://")
    database = parsed.path.lstrip("/")
    if not parsed.hostname or not database or not parsed.username:
        raise RuntimeError("Hosted Postgres DSNs must include user, host, and database name")
    query = tuple(
        sorted((key.lower(), tuple(sorted(value))) for key, value in parse_qs(parsed.query).items())
    )
    return (
        "postgresql",
        parsed.hostname.lower(),
        parsed.port or 5432,
        database,
        parsed.username,
        query,
    )


def _assert_redis_url_tls(name: str) -> None:
    parsed = urlparse(os.environ.get(name, "").strip())
    if parsed.scheme != "rediss":
        raise RuntimeError(f"{name} must use rediss:// in hosted mode")


def _reject_hosted_unsafe_secrets() -> None:
    if os.environ.get("OMNICLAW_HOSTED_DEFAULT_API_KEY", "").strip():
        raise RuntimeError(
            "OMNICLAW_HOSTED_DEFAULT_API_KEY is not supported in hosted mode; "
            "issue seller-scoped keys through the control plane"
        )
    local_values = {
        "OMNICLAW_OPS_SESSION_SECRET": "local-ops-console-session-secret-change-before-production",
    }
    for name, local_value in local_values.items():
        if os.environ.get(name, "").strip() == local_value:
            raise RuntimeError(f"{name} uses a non-production secret in hosted mode")


def _is_evm_network(network: str) -> bool:
    prefix = "eip155:"
    if not network.startswith(prefix):
        return False
    chain_id = network[len(prefix) :]
    return bool(chain_id) and chain_id.isdigit()


def _assert_hosted_exact_signer_env_safe(signer_env: Any) -> None:
    mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    if mode in {"", "local", "test"}:
        return
    if signer_env.signer_config.min_native_balance_wei is None:
        raise RuntimeError(
            "Hosted exact settlement requires OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI"
        )


def _hosted_exact_rpc_url(
    network: str,
    suffix: str,
    exact_config: ExactFacilitatorConfig,
    *,
    multi_network: bool,
) -> str:
    scoped_env = f"OMNICLAW_HOSTED_EXACT_RPC_URL_{suffix}"
    scoped_value = os.environ.get(scoped_env, "").strip()
    mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    if mode not in {"", "local", "test"}:
        if not scoped_value:
            raise RuntimeError(f"Missing hosted exact RPC URL for {network}. Set {scoped_env}")
        _assert_hosted_url_safe(scoped_env)
    if scoped_value:
        return scoped_value
    if multi_network:
        raise RuntimeError(f"Missing hosted exact RPC URL for {network}. Set {scoped_env}")
    return exact_config.rpc_url


def _network_env_suffix(network: str) -> str:
    suffix = []
    for char in network.upper():
        suffix.append(char if char.isalnum() else "_")
    return "".join(suffix)


def _postgres_store_factory_from_env_if_configured() -> Callable[[], Any] | None:
    dsn = os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if dsn:
        if not os.environ.get("OMNICLAW_HOSTED_POSTGRES_POOL_SIZE", "").strip():
            return postgres_store_from_env
        pool_size = _env_int_or_default("OMNICLAW_HOSTED_POSTGRES_POOL_SIZE", 10)
        checkout_timeout = _env_float_or_default(
            "OMNICLAW_HOSTED_POSTGRES_POOL_CHECKOUT_SECONDS",
            2.0,
        )
        return PostgresSettlementStorePool(
            dsn,
            max_size=pool_size,
            checkout_timeout_seconds=checkout_timeout,
        )
    return None


def _hosted_exact_provider_timeout_seconds() -> float:
    raw = os.environ.get("OMNICLAW_HOSTED_EXACT_PROVIDER_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            "OMNICLAW_HOSTED_EXACT_PROVIDER_TIMEOUT_SECONDS must be a number"
        ) from exc
    if value <= 0:
        raise RuntimeError("OMNICLAW_HOSTED_EXACT_PROVIDER_TIMEOUT_SECONDS must be positive")
    return value


def _hosted_exact_max_concurrent_settlements() -> int:
    return _env_int_or_default("OMNICLAW_HOSTED_EXACT_MAX_CONCURRENT_SETTLEMENTS", 1)


def _env_int_or_default(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _env_float_or_default(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _postgres_control_plane_state_from_env_if_configured() -> Any | None:
    if (
        os.getenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN")
        or os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN")
        or os.getenv("DATABASE_URL")
    ):
        return postgres_control_plane_state_from_env()
    return None


def _rate_limiter_from_env_if_configured() -> Any | None:
    if os.getenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL"):
        return RedisFixedWindowRateLimiter()
    return None


def _postgres_seller_account_resolver_from_env_if_configured() -> Any | None:
    if (
        os.getenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN")
        or os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN")
        or os.getenv("DATABASE_URL")
        or os.getenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_FROM_POSTGRES") == "true"
    ):
        return postgres_seller_account_resolver_from_env()
    return None


def _preflight_storage(
    store_factory: Callable[[], Any] | None,
    control_plane_state: Any | None,
    seller_account_resolver: Any | None,
) -> None:
    store = store_factory() if store_factory is not None else None
    try:
        _initialize_resources_or_close(store, control_plane_state, seller_account_resolver)
    except Exception:
        raise
    else:
        if store is not None:
            _close_resource(store)


def _initialize_resources_or_close(*resources: Any | None) -> None:
    initialized: list[Any] = []
    current: Any | None = None
    try:
        for resource in resources:
            current = resource
            if _initialize_resource(resource):
                initialized.append(resource)
    except Exception:
        _close_resource(current)
        for resource in reversed(initialized):
            if resource is not current:
                _close_resource(resource)
        raise


def _bootstrap_seller_accounts(
    seller_account_resolver: Any | None, seller_accounts: list[SellerAccountConfig] | None
) -> None:
    if seller_account_resolver is None or not seller_accounts:
        return
    bootstrap = getattr(seller_account_resolver, "bootstrap_seller_accounts", None)
    if callable(bootstrap):
        bootstrap(seller_accounts)


def _initialize_resource(resource: Any | None) -> bool:
    if resource is None:
        return False
    initialize = getattr(resource, "initialize", None)
    if initialize is None:
        return False
    initialize()
    return True


def _close_resource(resource: Any | None) -> None:
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if close is not None:
        close()


def reconciliation_owner_from_env(default_prefix: str = "hosted-reconciler") -> str:
    configured = os.getenv("OMNICLAW_HOSTED_RECONCILER_OWNER", "").strip()
    if configured:
        return configured
    hostname = os.getenv("HOSTNAME", "").strip()
    return f"{default_prefix}-{hostname}" if hostname else default_prefix


def _add_stats(target: ReconciliationRunStats, source: ReconciliationRunStats) -> None:
    target.scanned += source.scanned
    target.claimed += source.claimed
    target.settled += source.settled
    target.failed += source.failed
    target.marked_unknown += source.marked_unknown
    target.manual_review += source.manual_review
    target.pending += source.pending
    target.skipped += source.skipped
    target.errors += source.errors
