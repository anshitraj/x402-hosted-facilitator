from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


class _FakeCursor:
    def __init__(self, rows_by_query: list[list[tuple]]):
        self._rows_by_query = rows_by_query
        self._current_rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, _query):
        self._current_rows = self._rows_by_query.pop(0)

    def fetchall(self):
        return self._current_rows

    def fetchone(self):
        return self._current_rows[0]


class _FakeConnection:
    def __init__(self, rows_by_query: list[list[tuple]]):
        self._rows_by_query = rows_by_query

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return _FakeCursor(self._rows_by_query)


def _load_exporter():
    service_root = Path(__file__).resolve().parents[1]
    src_path = service_root / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    path = service_root / "scripts" / "hosted_metrics_exporter.py"
    spec = importlib.util.spec_from_file_location("hosted_metrics_exporter", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rows_to_metrics_exposes_production_alert_inputs(monkeypatch):
    exporter = _load_exporter()
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://metrics:metrics@postgres:5432/omniclaw",
    )

    rows_by_query = [
        [("exact_evm", "eip155:5042002", "settled", 7)],
        [("failed", 2)],
        [("unknown", 311)],
        [(9,)],
        [(3,)],
        [("circle_gateway", "eip155:5042002", 1)],
        [("circle_gateway", "eip155:5042002", 2)],
        [("exact_evm", "eip155:5042002", 11)],
    ]

    def fake_connect(dsn, connect_timeout):
        assert dsn == "postgresql://metrics:metrics@postgres:5432/omniclaw"
        assert connect_timeout == 3
        return _FakeConnection(rows_by_query)

    monkeypatch.setattr(exporter.psycopg, "connect", fake_connect)

    metrics = "\n".join(exporter._rows_to_metrics())

    assert (
        'omniclaw_hosted_settlement_records_total{network="eip155:5042002",provider="exact_evm",status="settled"} 7'
        in metrics
    )
    assert (
        'omniclaw_hosted_settlement_recent_failures_total{network="eip155:5042002",provider="circle_gateway"} 1'
        in metrics
    )
    assert (
        'omniclaw_hosted_unknown_settlements_recent_total{network="eip155:5042002",provider="circle_gateway"} 2'
        in metrics
    )
    assert (
        'omniclaw_hosted_duplicate_settle_claims_recent_total{network="eip155:5042002",provider="exact_evm"} 11'
        in metrics
    )
    assert rows_by_query == []


def test_collect_metrics_success_uses_operator_ids_not_wallet_labels(monkeypatch):
    exporter = _load_exporter()

    signer_address = "0x4cfdD69a2A89B91f3c12588085f098C268Ea8631"
    canary_address = "0xA6b9b6244a5Ad5FC2EF2BEB67cE04B75A0db91D7"

    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", signer_address)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "1000")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", canary_address)
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ID", "arc-gateway-canary")
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "1000")

    monkeypatch.setattr(
        exporter,
        "_rows_to_metrics",
        lambda: [
            exporter._line(
                "omniclaw_hosted_settlement_records_total",
                1,
                {"provider": "exact_evm", "network": "eip155:5042002", "status": "settled"},
            )
        ],
    )
    monkeypatch.setattr(
        exporter,
        "_heartbeat_metrics",
        lambda: ["omniclaw_hosted_reconciler_heartbeat_age_seconds 1"],
    )
    monkeypatch.setattr(
        exporter, "_facilitator_metrics", lambda: ["omniclaw_hosted_facilitator_up 1"]
    )

    class _FakeEth:
        def get_balance(self, address):
            assert address == signer_address
            return 2000

    class _FakeWeb3:
        @staticmethod
        def HTTPProvider(rpc, request_kwargs=None):
            assert rpc == "https://rpc.example.test"
            assert request_kwargs == {"timeout": 5}
            return object()

        @staticmethod
        def to_checksum_address(address):
            return address

        def __init__(self, provider):
            del provider
            self.eth = _FakeEth()

    class _FakeNanopaymentClient:
        def __init__(self, environment, timeout):
            assert environment == "testnet"
            assert timeout == 5

        async def check_balance(self, address, network):
            assert address == canary_address
            assert network == "eip155:5042002"
            return SimpleNamespace(available=2000, total=3000)

    monkeypatch.setattr(exporter, "Web3", _FakeWeb3)
    monkeypatch.setattr(exporter, "NanopaymentClient", _FakeNanopaymentClient)

    metrics = exporter.collect_metrics()

    assert 'signer_id="hosted-exact-alpha"' in metrics
    assert 'canary_id="arc-gateway-canary"' in metrics
    assert "omniclaw_hosted_exact_signer_above_min" in metrics
    assert "omniclaw_hosted_gateway_canary_above_min" in metrics
    assert "omniclaw_hosted_metrics_exporter_scrape_success 1" in metrics
    assert signer_address not in metrics
    assert canary_address not in metrics


def test_collect_metrics_failure_reports_error_type_without_message(monkeypatch):
    exporter = _load_exporter()

    def _raise_secret_error():
        raise RuntimeError("secret private-key-like value leaked")

    monkeypatch.setattr(exporter, "_rows_to_metrics", _raise_secret_error)

    metrics = exporter.collect_metrics()

    assert "omniclaw_hosted_metrics_exporter_scrape_success 0" in metrics
    assert (
        'omniclaw_hosted_metrics_collector_last_error{collector="settlement_store",error="RuntimeError"} 1'
        in metrics
    )
    assert "secret private-key-like value leaked" not in metrics


def test_missing_signer_and_gateway_config_emit_failed_floor_metrics(monkeypatch):
    exporter = _load_exporter()

    monkeypatch.delenv("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", raising=False)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_ID", "hosted-exact-alpha")
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ID", "arc-gateway-canary")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_SIGNER_MIN_GAS_WEI", "1000")
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "1000")

    assert (
        'omniclaw_hosted_exact_signer_configured{network="eip155:5042002",signer_id="hosted-exact-alpha"} 0'
        in exporter._signer_metrics()
    )
    assert (
        'omniclaw_hosted_exact_signer_above_min{network="eip155:5042002",signer_id="hosted-exact-alpha"} 0'
        in exporter._signer_metrics()
    )
    assert (
        'omniclaw_hosted_gateway_canary_configured{canary_id="arc-gateway-canary",network="eip155:5042002"} 0'
        in exporter._gateway_metrics()
    )
    assert (
        'omniclaw_hosted_gateway_canary_above_min{canary_id="arc-gateway-canary",network="eip155:5042002"} 0'
        in exporter._gateway_metrics()
    )


def test_metric_label_values_are_escaped():
    exporter = _load_exporter()

    line = exporter._line("metric_name", 1, {"label": 'quoted"and\\split\nline'})

    assert line == 'metric_name{label="quoted\\"and\\\\split\\nline"} 1'


class _FakeReadyResponse:
    def __init__(self, *, status: int, body: str):
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def test_facilitator_metrics_default_probe_uses_internal_readiness(monkeypatch):
    exporter = _load_exporter()
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_HEALTH_URL", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_FACILITATOR_READY_URL", raising=False)

    def fake_urlopen(url, timeout):
        assert url == "http://hosted-facilitator:4022/internal/readyz"
        assert timeout == 3
        return _FakeReadyResponse(status=200, body='{"status":"ok"}')

    monkeypatch.setattr(exporter.request, "urlopen", fake_urlopen)

    assert exporter._facilitator_metrics() == ["omniclaw_hosted_facilitator_up 1"]


def test_facilitator_metrics_custom_ready_url_and_timeout(monkeypatch):
    exporter = _load_exporter()
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_READY_URL",
        "http://facilitator.example/internal/readyz",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_FACILITATOR_READY_TIMEOUT_SECONDS", "7")

    def fake_urlopen(url, timeout):
        assert url == "http://facilitator.example/internal/readyz"
        assert timeout == 7
        return _FakeReadyResponse(status=200, body='{"status":"ok"}')

    monkeypatch.setattr(exporter.request, "urlopen", fake_urlopen)

    assert exporter._facilitator_metrics() == ["omniclaw_hosted_facilitator_up 1"]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, '{"status":"ok"}'),
        (200, '{"status":"degraded"}'),
        (200, "not-json"),
    ],
)
def test_facilitator_metrics_fails_closed_for_unready_or_invalid_response(
    monkeypatch,
    status: int,
    body: str,
):
    exporter = _load_exporter()
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_HEALTH_URL", "http://facilitator/internal/readyz"
    )
    monkeypatch.setattr(
        exporter.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeReadyResponse(status=status, body=body),
    )

    assert exporter._facilitator_metrics() == ["omniclaw_hosted_facilitator_up 0"]


def test_facilitator_metrics_fails_closed_on_probe_exception(monkeypatch):
    exporter = _load_exporter()

    def failing_urlopen(*_args, **_kwargs):
        raise TimeoutError("ready probe timed out")

    monkeypatch.setattr(exporter.request, "urlopen", failing_urlopen)

    assert exporter._facilitator_metrics() == ["omniclaw_hosted_facilitator_up 0"]


def test_metrics_handler_ignores_client_disconnect_on_write():
    exporter = _load_exporter()
    exporter.collect_metrics = lambda: "omniclaw_hosted_metrics_exporter_scrape_success 1\n"

    class _BrokenWriter:
        def write(self, _body):
            raise BrokenPipeError()

    class _Handler:
        path = "/healthz"
        wfile = _BrokenWriter()

        def send_response(self, status):
            assert status == 200

        def send_header(self, _key, _value):
            return None

        def end_headers(self):
            return None

    exporter.MetricsHandler.do_GET(_Handler())


def test_metrics_handler_healthz_is_liveness_even_when_collectors_fail():
    exporter = _load_exporter()
    exporter.collect_metrics = lambda: "omniclaw_hosted_metrics_exporter_scrape_success 0\n"

    class _Writer:
        body = b""

        def write(self, body):
            self.body += body

    class _Handler:
        path = "/healthz"
        wfile = _Writer()
        status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            return None

        def end_headers(self):
            return None

    handler = _Handler()

    exporter.MetricsHandler.do_GET(handler)

    assert handler.status == 200
    assert handler.wfile.body == b"ok\n"


def test_metrics_readyz_requires_signer_and_gateway_canary_in_strict_mode(monkeypatch):
    exporter = _load_exporter()
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://metrics:secret@pg.example.test:5432/omniclaw?sslmode=require",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_METRICS_STRICT_READY", "true")
    monkeypatch.delenv("OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS", raising=False)
    monkeypatch.delenv("OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS", raising=False)
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example.test")
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "1000")

    errors = exporter.validate_runtime_config()

    assert "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS must be configured" in errors
    assert "OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS must be configured" in errors


def test_metrics_readyz_accepts_strict_alpha_config(monkeypatch):
    exporter = _load_exporter()
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN",
        "postgresql://metrics:secret@pg.example.test:5432/omniclaw?sslmode=require",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_METRICS_STRICT_READY", "true")
    monkeypatch.setenv("OMNICLAW_HOSTED_EXACT_RPC_URL_EIP155_5042002", "https://rpc.example.test")
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_EXACT_SIGNER_ADDRESS",
        "0x1111111111111111111111111111111111111111",
    )
    monkeypatch.setenv(
        "OMNICLAW_HOSTED_GATEWAY_CANARY_ADDRESS",
        "0x2222222222222222222222222222222222222222",
    )
    monkeypatch.setenv("OMNICLAW_HOSTED_GATEWAY_CANARY_MIN_ATOMIC", "1000")

    assert exporter.validate_runtime_config() == []
