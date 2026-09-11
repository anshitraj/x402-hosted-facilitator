from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from hosted_facilitator.hosted.providers.base import HostedProviderUnavailableError

HOSTED_EXACT_PROVIDER_NAME = "exact_evm"
HOSTED_EXACT_SIGNER_ID_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_ID"
HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV"
HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_PRIVATE_KEY"
HOSTED_EXACT_SIGNER_STATUS_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_STATUS"
HOSTED_EXACT_SIGNER_MIN_GAS_WEI_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI"
HOSTED_EXACT_SIGNER_ENVIRONMENT_ENV = "OMNICLAW_HOSTED_EXACT_SIGNER_ENVIRONMENT"


class HostedSignerStatus(str, Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    PAUSED = "paused"
    DISABLED = "disabled"


@dataclass(frozen=True)
class HostedSignerConfig:
    signer_id: str
    provider: str
    network: str
    environment: str
    status: HostedSignerStatus = HostedSignerStatus.ACTIVE
    min_native_balance_wei: int | None = None
    secret_env_var: str | None = None

    def safe_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "signerId": self.signer_id,
            "provider": self.provider,
            "network": self.network,
            "environment": self.environment,
            "signerStatus": self.status.value,
        }
        if self.min_native_balance_wei is not None:
            metadata["minBalanceWei"] = self.min_native_balance_wei
        if self.secret_env_var:
            metadata["secretEnvVar"] = self.secret_env_var
        return metadata


class HostedSignerGuard:
    def __init__(
        self,
        config: HostedSignerConfig,
        *,
        balance_checker: Callable[[], Any] | None = None,
    ):
        self._config = config
        self._balance_checker = balance_checker
        self._settlement_locks: dict[str, asyncio.Lock] = {}
        self._settlement_locks_guard = asyncio.Lock()

    @property
    def config(self) -> HostedSignerConfig:
        return self._config

    def with_balance_checker(self, balance_checker: Callable[[], Any]) -> HostedSignerGuard:
        return HostedSignerGuard(self._config, balance_checker=balance_checker)

    @asynccontextmanager
    async def settlement_slot(self, *, network: str | None):
        lock = await self._lock_for_network(network or self._config.network)
        async with lock:
            yield

    async def _lock_for_network(self, network: str) -> asyncio.Lock:
        async with self._settlement_locks_guard:
            lock = self._settlement_locks.get(network)
            if lock is None:
                lock = asyncio.Lock()
                self._settlement_locks[network] = lock
            return lock

    async def preflight_settlement(
        self,
        *,
        balance_checker: Callable[[], Any] | None = None,
    ) -> None:
        health = await self.health_check(balance_checker=balance_checker)
        if health.get("status") != "ok":
            raise HostedProviderUnavailableError(
                str(health.get("errorType") or "SignerUnavailable")
            )

    async def health_check(
        self,
        *,
        balance_checker: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        health = self._config.safe_metadata()
        health["status"] = "ok"
        if self._config.status is not HostedSignerStatus.ACTIVE:
            health["status"] = "unhealthy"
            health["errorType"] = "SignerNotActive"
            return health

        if self._config.min_native_balance_wei is None:
            return health

        checker = balance_checker or self._balance_checker
        if checker is None:
            health["status"] = "unhealthy"
            health["errorType"] = "SignerBalanceUnavailable"
            return health

        try:
            balance = checker()
            if inspect.isawaitable(balance):
                balance = await balance
            balance_wei = int(balance)
        except Exception as exc:
            health["status"] = "unhealthy"
            health["errorType"] = type(exc).__name__
            return health

        health["balanceWei"] = balance_wei
        if balance_wei < self._config.min_native_balance_wei:
            health["status"] = "unhealthy"
            health["errorType"] = "SignerGasBelowMinimum"
        return health


@dataclass(frozen=True)
class HostedExactSignerEnv:
    signer_config: HostedSignerConfig
    private_key_env_var: str
    private_key: str


def load_hosted_exact_signer_env(
    *,
    default_network: str,
) -> HostedExactSignerEnv:
    private_key_env_var = _env(
        HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV_ENV,
        HOSTED_EXACT_SIGNER_PRIVATE_KEY_ENV,
    )
    signer_id = _required_env(HOSTED_EXACT_SIGNER_ID_ENV)
    private_key = _required_env(private_key_env_var)
    status = _signer_status(_env(HOSTED_EXACT_SIGNER_STATUS_ENV, HostedSignerStatus.ACTIVE.value))
    min_balance = _optional_int_env(HOSTED_EXACT_SIGNER_MIN_GAS_WEI_ENV)
    environment = _env(
        HOSTED_EXACT_SIGNER_ENVIRONMENT_ENV,
        _env("OMNICLAW_HOSTED_FACILITATOR_MODE", "local"),
    )
    return HostedExactSignerEnv(
        signer_config=HostedSignerConfig(
            signer_id=signer_id,
            provider=HOSTED_EXACT_PROVIDER_NAME,
            network=default_network,
            environment=environment,
            status=status,
            min_native_balance_wei=min_balance,
            secret_env_var=private_key_env_var,
        ),
        private_key_env_var=private_key_env_var,
        private_key=private_key,
    )


def with_signer_private_key(config: Any, private_key: str) -> Any:
    return replace(config, private_key=private_key)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _required_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    missing = ", ".join(names)
    raise RuntimeError(f"Missing required hosted signer environment variable: {missing}")


def _optional_int_env(*names: str) -> int | None:
    raw = ""
    name = names[0]
    for candidate in names:
        value = os.environ.get(candidate, "").strip()
        if value:
            raw = value
            name = candidate
            break
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer number of wei") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


def _signer_status(raw: str) -> HostedSignerStatus:
    try:
        return HostedSignerStatus(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(status.value for status in HostedSignerStatus)
        raise RuntimeError(f"{HOSTED_EXACT_SIGNER_STATUS_ENV} must be one of: {allowed}") from exc
