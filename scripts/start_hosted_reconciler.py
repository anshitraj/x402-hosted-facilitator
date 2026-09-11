#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import timedelta
from pathlib import Path
from typing import Any

from hosted_facilitator.hosted.runner import (
    HostedReconciliationRunner,
    create_hosted_exact_reconciler_from_env,
    reconciliation_owner_from_env,
)
from hosted_facilitator.hosted.storage import utcnow
from hosted_facilitator.hosted.telemetry import HostedTelemetry

HEARTBEAT_PATH = Path(
    os.environ.get(
        "OMNICLAW_HOSTED_RECONCILER_HEARTBEAT_PATH",
        "/tmp/omniclaw-reconciler-heartbeat.json",
    )
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    value = int(raw)
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _env_timedelta_seconds(name: str, default: timedelta) -> timedelta:
    return timedelta(seconds=_env_float(name, default.total_seconds()))


def _write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    HEARTBEAT_PATH.write_text(
        json.dumps(
            {
                "status": status,
                "updatedAt": utcnow().isoformat(),
                **(payload or {}),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


async def main() -> None:
    owner = reconciliation_owner_from_env()
    hosted_mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    if not hosted_mode:
        raise RuntimeError("OMNICLAW_HOSTED_FACILITATOR_MODE must be set for reconciler startup")
    telemetry = HostedTelemetry.configure_process(hosted_mode=hosted_mode)
    reconciler, store = create_hosted_exact_reconciler_from_env(
        owner=owner,
        lease_seconds=_env_int("OMNICLAW_HOSTED_RECONCILER_LEASE_SECONDS", 60),
        stale_in_progress_after=_env_timedelta_seconds(
            "OMNICLAW_HOSTED_RECONCILER_STALE_IN_PROGRESS_SECONDS",
            timedelta(minutes=2),
        ),
        stale_submitted_after=_env_timedelta_seconds(
            "OMNICLAW_HOSTED_RECONCILER_STALE_SUBMITTED_SECONDS",
            timedelta(minutes=2),
        ),
        stale_unknown_after=_env_timedelta_seconds(
            "OMNICLAW_HOSTED_RECONCILER_STALE_UNKNOWN_SECONDS",
            timedelta(minutes=5),
        ),
        manual_review_after=_env_timedelta_seconds(
            "OMNICLAW_HOSTED_RECONCILER_MANUAL_REVIEW_SECONDS",
            timedelta(minutes=30),
        ),
        limit=_env_int("OMNICLAW_HOSTED_RECONCILER_LIMIT", 100),
    )
    runner = HostedReconciliationRunner(
        reconciler=reconciler,
        interval_seconds=_env_float("OMNICLAW_HOSTED_RECONCILER_INTERVAL_SECONDS", 5.0),
        error_interval_seconds=_env_float(
            "OMNICLAW_HOSTED_RECONCILER_ERROR_INTERVAL_SECONDS",
            30.0,
        ),
        on_iteration=lambda stats: (
            _write_heartbeat(
                "ok",
                {
                    "owner": owner,
                    "scanned": stats.scanned,
                    "claimed": stats.claimed,
                    "settled": stats.settled,
                    "failed": stats.failed,
                    "manualReview": stats.manual_review,
                    "pending": stats.pending,
                    "errors": stats.errors,
                },
            ),
            telemetry.log_event(
                "INFO",
                "hosted_reconciler_iteration",
                {
                    "owner": owner,
                    "scanned": stats.scanned,
                    "claimed": stats.claimed,
                    "settled": stats.settled,
                    "failed": stats.failed,
                    "manual_review": stats.manual_review,
                    "pending": stats.pending,
                    "errors": stats.errors,
                },
            ),
        ),
    )

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, runner.stop)

    _write_heartbeat("starting", {"owner": owner})
    try:
        stats = await runner.run()
        _write_heartbeat(
            "stopped",
            {
                "owner": owner,
                "runs": stats.runs,
                "failedRuns": stats.failed_runs,
                "lastError": stats.last_error,
            },
        )
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        await telemetry.close()


if __name__ == "__main__":
    asyncio.run(main())
