#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request

import psycopg
from web3 import Web3

from hosted_facilitator.nanopayments.client import NanopaymentClient

HEARTBEAT_PATH = Path(
    os.environ.get(
        "OMNICLAW_HOSTED_RECONCILER_HEARTBEAT_PATH",
        "/tmp/omniclaw-reconciler-heartbeat.json",
    )
)
_EVM_ADDRESS_RE = "0x"


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _first_csv_env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    return value.split(",", 1)[0].strip() or default


def _strict_readiness_enabled() -> bool:
    value = os.environ.get("OMNICLAW_HOSTED_METRICS_STRICT_READY", "").strip().lower()
    if value:
        return value in {"1", "true", "yes", "on"}
    mode = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_MODE", "").strip().lower()
    return mode in {"production", "staging", "alpha"}


def validate_runtime_config(*, strict: bool | None = None) -> list[str]:
    strict = _strict_readiness_enabled() if strict is None else strict
    errors: list[str] = []
    if not os.environ.get("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "").strip():
        errors.append("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN is required")
    if not strict:
        return errors
    signer_address = os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "").strip()
    signer_rpc = os.environ.get("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "").strip()
    gateway_canary = os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", "").strip()
    if not _is_evm_address(signer_address):
        errors.append("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS must be configured")
    if not signer_rpc.startswith("https://"):
        errors.append("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002 must use https")
    if not _is_evm_address(gateway_canary):
        errors.append("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS must be configured")
    if _env_int("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", 0) <= 0:
        errors.append("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC must be positive")
    return errors


def _is_evm_address(value: str) -> bool:
    return (
        len(value) == 42
        and value.startswith(_EVM_ADDRESS_RE)
        and all(ch in "0123456789abcdefABCDEF" for ch in value[2:])
    )


def _dsn() -> str:
    dsn = os.environ.get("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN", "").strip()
    if not dsn:
        raise RuntimeError("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN is required")
    return dsn


def _line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if not labels:
        return f"{name} {value}"
    rendered = ",".join(f'{key}="{_escape_label(label)}"' for key, label in sorted(labels.items()))
    return f"{name}{{{rendered}}} {value}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _rows_to_metrics() -> list[str]:
    metrics: list[str] = []
    with psycopg.connect(_dsn(), connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select provider, coalesce(network, ''), status, count(*)
                from settlement_records
                group by provider, coalesce(network, ''), status
                """
            )
            for provider, network, status, count in cur.fetchall():
                metrics.append(
                    _line(
                        "omniclaw_hosted_settlement_records_total",
                        int(count),
                        {
                            "provider": str(provider),
                            "network": str(network),
                            "status": str(status),
                        },
                    )
                )

            cur.execute("select status, count(*) from settlement_attempts group by status")
            for status, count in cur.fetchall():
                metrics.append(
                    _line(
                        "omniclaw_hosted_settlement_attempts_total",
                        int(count),
                        {"status": str(status)},
                    )
                )

            cur.execute(
                """
                select status, extract(epoch from (now() - min(updated_at)))::bigint
                from settlement_records
                where status in ('submitted', 'unknown', 'manual_review', 'settle_in_progress')
                group by status
                """
            )
            seen_statuses = set()
            for status, age in cur.fetchall():
                seen_statuses.add(str(status))
                metrics.append(
                    _line(
                        "omniclaw_hosted_settlement_oldest_age_seconds",
                        int(age or 0),
                        {"status": str(status)},
                    )
                )
            for status in ("submitted", "unknown", "manual_review", "settle_in_progress"):
                if status not in seen_statuses:
                    metrics.append(
                        _line(
                            "omniclaw_hosted_settlement_oldest_age_seconds",
                            0,
                            {"status": status},
                        )
                    )

            cur.execute("select count(*) from settlement_records")
            metrics.append(
                _line("omniclaw_hosted_settlement_records_total_all", int(cur.fetchone()[0]))
            )

            cur.execute("select count(*) from settlement_attempts")
            metrics.append(
                _line("omniclaw_hosted_settlement_attempts_total_all", int(cur.fetchone()[0]))
            )

            cur.execute(
                """
                select r.provider, coalesce(r.network, ''), count(*)
                from settlement_attempts a
                join settlement_records r on r.id = a.settlement_record_id
                where a.status = 'failed'
                  and coalesce(a.finished_at, a.started_at) > now() - interval '15 minutes'
                group by r.provider, coalesce(r.network, '')
                """
            )
            for provider, network, count in cur.fetchall():
                metrics.append(
                    _line(
                        "omniclaw_hosted_settlement_recent_failures_total",
                        int(count),
                        {"provider": str(provider), "network": str(network)},
                    )
                )

            cur.execute(
                """
                select provider, coalesce(network, ''), count(*)
                from settlement_records
                where status = 'unknown'
                  and updated_at > now() - interval '15 minutes'
                group by provider, coalesce(network, '')
                """
            )
            for provider, network, count in cur.fetchall():
                metrics.append(
                    _line(
                        "omniclaw_hosted_unknown_settlements_recent_total",
                        int(count),
                        {"provider": str(provider), "network": str(network)},
                    )
                )

            cur.execute(
                """
                select provider, coalesce(network, ''), coalesce(sum(duplicate_claims), 0)
                from settlement_records
                where last_duplicate_at > now() - interval '15 minutes'
                group by provider, coalesce(network, '')
                """
            )
            for provider, network, count in cur.fetchall():
                metrics.append(
                    _line(
                        "omniclaw_hosted_duplicate_settle_claims_recent_total",
                        int(count),
                        {"provider": str(provider), "network": str(network)},
                    )
                )

    return metrics


def _heartbeat_metrics() -> list[str]:
    metrics: list[str] = []
    if not HEARTBEAT_PATH.exists():
        return [
            _line("omniclaw_hosted_reconciler_heartbeat_age_seconds", 999999),
            _line("omniclaw_hosted_reconciler_heartbeat_status", 0, {"status": "missing"}),
        ]
    data = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    updated_at = datetime.fromisoformat(str(data["updatedAt"]))
    age = max(0.0, (datetime.now(timezone.utc) - updated_at).total_seconds())
    status = str(data.get("status", "unknown"))
    metrics.append(_line("omniclaw_hosted_reconciler_heartbeat_age_seconds", round(age, 3)))
    metrics.append(_line("omniclaw_hosted_reconciler_heartbeat_status", 1, {"status": status}))
    for key, metric in {
        "scanned": "omniclaw_hosted_reconciler_last_scanned",
        "claimed": "omniclaw_hosted_reconciler_last_claimed",
        "settled": "omniclaw_hosted_reconciler_last_settled",
        "failed": "omniclaw_hosted_reconciler_last_failed",
        "manualReview": "omniclaw_hosted_reconciler_last_manual_review",
        "pending": "omniclaw_hosted_reconciler_last_pending",
        "errors": "omniclaw_hosted_reconciler_last_errors",
    }.items():
        metrics.append(_line(metric, int(data.get(key, 0) or 0)))
    return metrics


def _signer_metrics() -> list[str]:
    address = os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", "").strip()
    rpc = os.environ.get("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "").strip()
    network = os.environ.get(
        "OMNICLAW_HOSTED_EXACT_SIGNER_NETWORK",
        _first_csv_env("OMNICLAW_HOSTED_EXACT_NETWORKS", "eip155:5042002"),
    )
    labels = {
        "network": network,
        "signer_id": os.environ.get("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "default"),
    }
    min_wei = _env_int("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", 0)
    if not address or not rpc:
        return [
            _line("omniclaw_hosted_exact_signer_configured", 0, labels),
            _line("omniclaw_hosted_exact_signer_native_wei", 0, labels),
            _line("omniclaw_hosted_exact_signer_min_native_wei", min_wei, labels),
            _line("omniclaw_hosted_exact_signer_above_min", 0, labels),
        ]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 5}))
    native_wei = int(w3.eth.get_balance(Web3.to_checksum_address(address)))
    return [
        _line("omniclaw_hosted_exact_signer_configured", 1, labels),
        _line("omniclaw_hosted_exact_signer_native_wei", native_wei, labels),
        _line("omniclaw_hosted_exact_signer_min_native_wei", min_wei, labels),
        _line("omniclaw_hosted_exact_signer_above_min", 1 if native_wei >= min_wei else 0, labels),
    ]


async def _gateway_metrics_async() -> list[str]:
    address = os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", "").strip()
    network = os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_NETWORK", "eip155:5042002")
    min_atomic = _env_int("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", 1000)
    labels = {
        "network": network,
        "canary_id": os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_ID", "gateway-canary"),
    }
    if not address:
        return [
            _line("omniclaw_hosted_gateway_canary_configured", 0, labels),
            _line("omniclaw_hosted_gateway_canary_available_atomic", 0, labels),
            _line("omniclaw_hosted_gateway_canary_total_atomic", 0, labels),
            _line("omniclaw_hosted_gateway_canary_min_atomic", min_atomic, labels),
            _line("omniclaw_hosted_gateway_canary_above_min", 0, labels),
        ]
    client = NanopaymentClient(
        environment=os.environ.get("OMNICLAW_HOSTED_CIRCLE_GATEWAY_ENVIRONMENT", "testnet"),
        timeout=float(os.environ.get("OMNICLAW_HOSTED_GATEWAY_CANARY_TIMEOUT_SECONDS", "5")),
    )
    balance = await client.check_balance(Web3.to_checksum_address(address), network)
    return [
        _line("omniclaw_hosted_gateway_canary_configured", 1, labels),
        _line("omniclaw_hosted_gateway_canary_available_atomic", int(balance.available), labels),
        _line("omniclaw_hosted_gateway_canary_total_atomic", int(balance.total), labels),
        _line("omniclaw_hosted_gateway_canary_min_atomic", min_atomic, labels),
        _line(
            "omniclaw_hosted_gateway_canary_above_min",
            1 if int(balance.available) >= min_atomic else 0,
            labels,
        ),
    ]


def _gateway_metrics() -> list[str]:
    return asyncio.run(_gateway_metrics_async())


def _facilitator_metrics() -> list[str]:
    url = os.environ.get(
        "OMNICLAW_HOSTED_FACILITATOR_HEALTH_URL",
        os.environ.get(
            "OMNICLAW_HOSTED_FACILITATOR_READY_URL",
            "http://hosted-facilitator:4022/internal/readyz",
        ),
    )
    timeout = _env_int("OMNICLAW_HOSTED_FACILITATOR_READY_TIMEOUT_SECONDS", 3)
    try:
        with request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        ready = response.status == 200 and data.get("status") == "ok"
    except Exception:
        ready = False
    return [_line("omniclaw_hosted_facilitator_up", 1 if ready else 0)]


def _collect_section(name: str, collector) -> tuple[list[str], bool]:
    try:
        metrics = list(collector())
        metrics.append(_line("omniclaw_hosted_metrics_collector_success", 1, {"collector": name}))
        return metrics, True
    except Exception as exc:
        return [
            _line("omniclaw_hosted_metrics_collector_success", 0, {"collector": name}),
            _line(
                "omniclaw_hosted_metrics_collector_last_error",
                1,
                {"collector": name, "error": type(exc).__name__},
            ),
        ], False


def collect_metrics() -> str:
    metrics: list[str] = [
        "# HELP omniclaw_hosted_metrics_exporter_scrape_success Whether this scrape succeeded.",
        "# TYPE omniclaw_hosted_metrics_exporter_scrape_success gauge",
    ]
    successes = []
    for name, collector in (
        ("settlement_store", _rows_to_metrics),
        ("reconciler_heartbeat", _heartbeat_metrics),
        ("facilitator_health", _facilitator_metrics),
        ("exact_signer", _signer_metrics),
        ("gateway_canary", _gateway_metrics),
    ):
        section_metrics, ok = _collect_section(name, collector)
        metrics.extend(section_metrics)
        successes.append(ok)
    metrics.append(
        _line("omniclaw_hosted_metrics_exporter_scrape_success", 1 if all(successes) else 0)
    )
    return "\n".join(metrics) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/metrics", "/healthz", "/readyz"}:
            self.send_error(404)
            return
        if self.path == "/healthz":
            body = b"ok\n"
            content_type = "text/plain; charset=utf-8"
            status = 200
        elif self.path == "/readyz":
            errors = validate_runtime_config()
            status = 200 if not errors else 503
            body = json.dumps(
                {"status": "ok" if not errors else "error", "errors": errors},
                sort_keys=True,
            ).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        else:
            body = collect_metrics().encode("utf-8")
            content_type = "text/plain; version=0.0.4; charset=utf-8"
            status = 200
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    host = os.environ.get("OMNICLAW_HOSTED_METRICS_HOST", "0.0.0.0")
    port = _env_int("OMNICLAW_HOSTED_METRICS_PORT", 9108)
    ThreadingHTTPServer((host, port), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
