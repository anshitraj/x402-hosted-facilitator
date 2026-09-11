from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from typing import Any, Protocol


class HealthCheckable(Protocol):
    def health_check(self) -> bool | dict[str, Any]: ...


async def component_health(name: str, component: Any) -> dict[str, Any]:
    started = time.perf_counter()
    checker = getattr(component, "health_check", None)
    if checker is None:
        return {
            "name": name,
            "status": "unknown",
            "latencyMs": _elapsed_ms(started),
        }
    try:
        result = checker()
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        return {
            "name": name,
            "status": "unhealthy",
            "errorType": type(exc).__name__,
            "latencyMs": _elapsed_ms(started),
        }
    if isinstance(result, Mapping):
        status = str(result.get("status", "ok"))
        safe_keys = {
            "status",
            "count",
            "providerCount",
            "networkCount",
            "unhealthyNetworkCount",
            "errorType",
            "signerStatus",
            "network",
            "provider",
            "environment",
            "balanceWei",
            "minBalanceWei",
            "maxConcurrentSettlements",
            "signerLockScope",
            "supportedNetworks",
        }
        safe_result = {}
        for key, value in result.items():
            if key not in safe_keys:
                continue
            if isinstance(value, str | int | float | bool | type(None)):
                safe_result[str(key)] = value
            elif key == "supportedNetworks" and isinstance(value, list | tuple):
                safe_result[str(key)] = tuple(str(item) for item in value)
        safe_result.update({"name": name, "status": status, "latencyMs": _elapsed_ms(started)})
        return safe_result
    return {
        "name": name,
        "status": "ok" if result else "unhealthy",
        "latencyMs": _elapsed_ms(started),
    }


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)
