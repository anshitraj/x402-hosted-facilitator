from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple, Protocol
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI
from opentelemetry import metrics, propagate, trace
from opentelemetry.context import attach, detach
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from hosted_facilitator.hosted.limits import (
    HostedRateLimiter,
    RateLimitDecision,
    RateLimitRequest,
)

OTEL_REQUIRED_ENV = "OMNICLAW_HOSTED_OTEL_REQUIRED"
OTEL_COLLECTOR_HEALTH_URL_ENV = "OMNICLAW_HOSTED_OTEL_COLLECTOR_HEALTH_URL"
OTEL_ALLOW_INSECURE_ENV = "OMNICLAW_HOSTED_OTEL_ALLOW_INSECURE"
OTEL_ALLOWED_TRACESTATE_KEYS_ENV = "OMNICLAW_HOSTED_OTEL_ALLOWED_TRACESTATE_KEYS"
JSON_LOGS_ENV = "OMNICLAW_HOSTED_JSON_LOGS"
LOG_LEVEL_ENV = "OMNICLAW_HOSTED_LOG_LEVEL"

_LOCAL_MODES = {"local", "test"}
_APPROVED_CAPTURE_HEADERS = {"traceparent", "tracestate", "x-request-id", "user-agent"}
_CAPTURE_HEADER_ENVS = (
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE",
)
_BODY_CAPTURE_ENVS = (
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_BODY",
    "OTEL_INSTRUMENTATION_ASGI_CAPTURE_BODY",
    "OTEL_PYTHON_INSTRUMENTATION_HTTP_CAPTURE_BODY",
)
_ALLOWED_ATTRIBUTE_KEYS = {
    "action",
    "decision",
    "deployment.environment",
    "duplicate",
    "endpoint",
    "error_type",
    "network",
    "omniclaw.correlation_id",
    "omniclaw.duplicate",
    "omniclaw.endpoint",
    "omniclaw.error_type",
    "omniclaw.network",
    "omniclaw.seller_account_id",
    "omniclaw.provider",
    "omniclaw.scheme",
    "omniclaw.status",
    "omniclaw.tenant_id",
    "provider",
    "reason",
    "scheme",
    "service.name",
    "status",
}
_ALLOWED_LOG_ATTRIBUTE_KEYS = {
    *_ALLOWED_ATTRIBUTE_KEYS,
    "duration_ms",
    "http.status_code",
    "omniclaw.duration_ms",
    "omniclaw.status_code",
}
_ALLOWED_METRIC_ATTRIBUTE_KEYS = {
    "action",
    "asset",
    "decision",
    "duplicate",
    "endpoint",
    "error_type",
    "network",
    "provider",
    "reason",
    "scheme",
    "signer_id",
    "status",
}
_OTEL_PROVIDERS_CONFIGURED = False
_JSON_LOGGING_CONFIGURED = False


class _ConfiguredProviders(NamedTuple):
    trace_provider: TracerProvider
    metric_provider: MeterProvider


class HostedTelemetryProtocol(Protocol):
    hosted_safe: bool

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, Any] | None = None): ...

    def set_attributes(self, span: Any, attributes: Mapping[str, Any] | None) -> None: ...

    def record_exception(self, span: Any, exc: Exception) -> None: ...

    def record_counter(self, name: str, attributes: Mapping[str, Any] | None = None) -> None: ...

    def record_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None: ...

    def log_event(
        self,
        level: str,
        event: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def health_check(self) -> bool | dict[str, Any]: ...


@dataclass(frozen=True)
class HostedTelemetryConfig:
    service_name: str = "omniclaw-hosted-facilitator"
    environment: str = "local"
    required: bool = False
    otlp_endpoint: str | None = None
    collector_health_url: str | None = None
    allow_insecure_otlp: bool = False
    allowed_tracestate_keys: tuple[str, ...] = ()
    json_logs_required: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, *, hosted_mode: str) -> HostedTelemetryConfig:
        return cls(
            service_name=os.getenv("OTEL_SERVICE_NAME", cls.service_name),
            environment=os.getenv(
                "OMNICLAW_HOSTED_FACILITATOR_ENV",
                os.getenv("OMNICLAW_HOSTED_FACILITATOR_MODE", hosted_mode),
            ),
            required=_truthy(os.getenv(OTEL_REQUIRED_ENV)),
            otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
            collector_health_url=os.getenv(OTEL_COLLECTOR_HEALTH_URL_ENV),
            allow_insecure_otlp=_truthy(os.getenv(OTEL_ALLOW_INSECURE_ENV)),
            allowed_tracestate_keys=_csv_env(OTEL_ALLOWED_TRACESTATE_KEYS_ENV),
            json_logs_required=_truthy(os.getenv(JSON_LOGS_ENV)),
            log_level=os.getenv(LOG_LEVEL_ENV, "INFO"),
        )

    def validate(self, *, hosted_mode: str) -> None:
        hosted = hosted_mode not in _LOCAL_MODES
        if hosted and not self.required:
            raise RuntimeError(f"{OTEL_REQUIRED_ENV}=true is required in hosted mode")
        if self.required and not self.otlp_endpoint:
            raise RuntimeError(
                "OTEL_EXPORTER_OTLP_ENDPOINT is required when hosted OTEL is required"
            )
        if self.required and not self.collector_health_url:
            raise RuntimeError(
                f"{OTEL_COLLECTOR_HEALTH_URL_ENV} is required when hosted OTEL is required"
            )
        if self.otlp_endpoint:
            parsed = urlparse(self.otlp_endpoint)
            if parsed.username or parsed.password:
                raise RuntimeError("OTEL OTLP endpoint must not include credentials")
            if hosted and parsed.scheme != "https":
                raise RuntimeError("OTEL OTLP endpoint must use https outside local/test mode")
        if self.collector_health_url:
            parsed = urlparse(self.collector_health_url)
            if parsed.username or parsed.password:
                raise RuntimeError("OTEL collector health URL must not include credentials")
            if not parsed.scheme or not parsed.netloc:
                raise RuntimeError("OTEL collector health URL must be absolute")
            if hosted and parsed.scheme != "https":
                raise RuntimeError(
                    "OTEL collector health URL must use https outside local/test mode"
                )
            if hosted and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                raise RuntimeError(
                    "OTEL collector health URL must not use localhost in hosted mode"
                )
        if hosted and not self.json_logs_required:
            raise RuntimeError(f"{JSON_LOGS_ENV}=true is required in hosted mode")
        _validate_unsafe_capture_env()


class HostedTelemetry:
    hosted_safe = True

    def __init__(
        self,
        *,
        config: HostedTelemetryConfig,
        providers: _ConfiguredProviders | None = None,
    ):
        self._config = config
        self._providers = providers
        self._tracer = trace.get_tracer("hosted_facilitator.hosted")
        self._meter = metrics.get_meter("hosted_facilitator.hosted")
        self._base_attributes = _safe_attributes(
            {
                "service.name": config.service_name,
                "deployment.environment": config.environment,
            }
        )
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._logger = logging.getLogger("hosted_facilitator.hosted")

    @classmethod
    def configure_app(
        cls,
        app: FastAPI,
        *,
        hosted_mode: str,
        config: HostedTelemetryConfig | None = None,
    ) -> HostedTelemetry:
        config = config or HostedTelemetryConfig.from_env(hosted_mode=hosted_mode)
        config.validate(hosted_mode=hosted_mode)
        propagate.set_global_textmap(TraceContextTextMapPropagator())
        providers = None
        if config.required:
            providers = _configure_otel_providers(config)
        if config.json_logs_required:
            configure_hosted_json_logging(config)
        app.middleware("http")(_trace_header_sanitizer(config))
        return cls(config=config, providers=providers)

    @classmethod
    def configure_process(
        cls,
        *,
        hosted_mode: str,
        config: HostedTelemetryConfig | None = None,
    ) -> HostedTelemetry:
        config = config or HostedTelemetryConfig.from_env(hosted_mode=hosted_mode)
        config.validate(hosted_mode=hosted_mode)
        propagate.set_global_textmap(TraceContextTextMapPropagator())
        providers = None
        if config.required:
            providers = _configure_otel_providers(config)
        if config.json_logs_required:
            configure_hosted_json_logging(config)
        return cls(config=config, providers=providers)

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, Any] | None = None):
        with self._tracer.start_as_current_span(name) as span:
            self.set_attributes(span, attributes)
            yield span

    def set_attributes(self, span: Any, attributes: Mapping[str, Any] | None) -> None:
        if not isinstance(span, Span):
            return
        try:
            for key, value in {**self._base_attributes, **_safe_attributes(attributes)}.items():
                span.set_attribute(key, value)
        except Exception:
            return

    def record_exception(self, span: Any, exc: Exception) -> None:
        if not isinstance(span, Span):
            return
        try:
            span.set_attribute("omniclaw.error_type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
        except Exception:
            return

    def record_counter(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        try:
            instrument = self._counters.get(name)
            if instrument is None:
                instrument = self._meter.create_counter(_safe_metric_name(name))
                self._counters[name] = instrument
            instrument.add(1, attributes=_safe_metric_attributes(attributes))
        except Exception:
            return

    def record_histogram(
        self,
        name: str,
        value: float,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            instrument = self._histograms.get(name)
            if instrument is None:
                instrument = self._meter.create_histogram(_safe_metric_name(name), unit="s")
                self._histograms[name] = instrument
            instrument.record(
                value,
                attributes=_safe_metric_attributes(attributes),
            )
        except Exception:
            return

    def log_event(
        self,
        level: str,
        event: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            self._logger.log(
                _log_level(level),
                _safe_log_event(event),
                extra={
                    "omniclaw_event": _safe_log_event(event),
                    "omniclaw_attrs": {
                        **self._base_attributes,
                        **_safe_log_attributes(attributes),
                    },
                },
            )
        except Exception:
            return

    async def health_check(self) -> bool | dict[str, Any]:
        if not self._config.required:
            return {"status": "ok"}
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(self._config.collector_health_url or "")
        except Exception as exc:
            return {"status": "unhealthy", "errorType": type(exc).__name__}
        return {"status": "ok" if 200 <= response.status_code < 300 else "unhealthy"}

    async def close(self) -> None:
        if self._providers is None:
            return
        with suppress(Exception):
            self._providers.trace_provider.shutdown()
        with suppress(Exception):
            self._providers.metric_provider.shutdown()


class TelemetryHostedRateLimiter:
    def __init__(self, inner: HostedRateLimiter, telemetry: HostedTelemetryProtocol):
        self._inner = inner
        self._telemetry = telemetry

    @property
    def hosted_safe(self) -> bool:
        return self._inner.hosted_safe

    async def initialize(self) -> None:
        initialize = getattr(self._inner, "initialize", None)
        if initialize is not None:
            result = initialize()
            if inspect.isawaitable(result):
                await result

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        decision = await self._inner.check(request)
        with suppress(Exception):
            self._telemetry.record_counter(
                "omniclaw.hosted.rate_limit.decisions",
                {
                    "endpoint": request.endpoint.value,
                    "decision": "allowed" if decision.allowed else "denied",
                },
            )
        return decision

    async def health_check(self) -> bool | dict[str, Any]:
        checker = getattr(self._inner, "health_check", None)
        if checker is None:
            return {"status": "unknown"}
        result = checker()
        if inspect.isawaitable(result):
            result = await result
        return result

    async def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def _configure_otel_providers(config: HostedTelemetryConfig) -> _ConfiguredProviders | None:
    global _OTEL_PROVIDERS_CONFIGURED

    if _OTEL_PROVIDERS_CONFIGURED:
        return None
    if config.otlp_endpoint is None:
        raise RuntimeError("OTEL_EXPORTER_OTLP_ENDPOINT is required to configure OTEL providers")
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "deployment.environment": config.environment,
        }
    )
    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=_otlp_http_signal_endpoint(config, "traces")))
    )
    metric_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=_otlp_http_signal_endpoint(config, "metrics"))
            )
        ],
    )
    trace.set_tracer_provider(trace_provider)
    metrics.set_meter_provider(metric_provider)
    _OTEL_PROVIDERS_CONFIGURED = True
    return _ConfiguredProviders(trace_provider=trace_provider, metric_provider=metric_provider)


def configure_hosted_json_logging(config: HostedTelemetryConfig) -> None:
    global _JSON_LOGGING_CONFIGURED

    root_logger = logging.getLogger()
    root_logger.setLevel(_log_level(config.log_level))
    formatter = HostedJsonLogFormatter(
        service_name=config.service_name,
        environment=config.environment,
    )
    for handler in list(root_logger.handlers):
        if not getattr(handler, "_omniclaw_hosted_json_handler", False):
            root_logger.removeHandler(handler)
    if _JSON_LOGGING_CONFIGURED:
        for handler in root_logger.handlers:
            if getattr(handler, "_omniclaw_hosted_json_handler", False):
                handler.setFormatter(formatter)
                handler.setLevel(_log_level(config.log_level))
        return
    handler = logging.StreamHandler(sys.stdout)
    handler._omniclaw_hosted_json_handler = True
    handler.setLevel(_log_level(config.log_level))
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    _JSON_LOGGING_CONFIGURED = True


class HostedJsonLogFormatter(logging.Formatter):
    def __init__(self, *, service_name: str, environment: str):
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": _safe_attribute_value(record.name),
            "event": _safe_log_event(getattr(record, "omniclaw_event", record.name)),
            "service.name": _safe_attribute_value(self._service_name),
            "deployment.environment": _safe_attribute_value(self._environment),
        }
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = f"{context.trace_id:032x}"
            payload["span_id"] = f"{context.span_id:016x}"
        attrs = getattr(record, "omniclaw_attrs", None)
        payload.update(_safe_log_attributes(attrs))
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _otlp_http_signal_endpoint(config: HostedTelemetryConfig, signal: str) -> str:
    endpoint = (config.otlp_endpoint or "").rstrip("/")
    if endpoint.endswith(f"/v1/{signal}"):
        return endpoint
    if endpoint.endswith("/v1"):
        return f"{endpoint}/{signal}"
    return f"{endpoint}/v1/{signal}"


def _trace_header_sanitizer(config: HostedTelemetryConfig):
    async def middleware(request, call_next):
        sanitized_headers = []
        allowed_tracestate_keys = set(config.allowed_tracestate_keys)
        for name, value in request.scope.get("headers", []):
            lower_name = name.lower()
            if lower_name == b"baggage":
                continue
            if lower_name == b"tracestate" and not _tracestate_allowed(
                value.decode("latin1", errors="ignore"),
                allowed_tracestate_keys,
            ):
                continue
            sanitized_headers.append((name, value))
        request.scope["headers"] = sanitized_headers
        token = None
        if config.required:
            carrier = {
                name.decode("latin1", errors="ignore"): value.decode("latin1", errors="ignore")
                for name, value in sanitized_headers
                if name.lower() in {b"traceparent", b"tracestate"}
            }
            token = attach(propagate.extract(carrier))
        try:
            return await call_next(request)
        finally:
            if token is not None:
                detach(token)

    return middleware


def _tracestate_allowed(value: str, allowed_keys: set[str]) -> bool:
    if len(value) > 512:
        return False
    if not value:
        return True
    if not allowed_keys:
        return False
    keys = [part.split("=", 1)[0].strip() for part in value.split(",") if part.strip()]
    return all(key in allowed_keys for key in keys)


def _validate_unsafe_capture_env() -> None:
    for env_name in _CAPTURE_HEADER_ENVS:
        configured = _csv_env(env_name)
        unsafe = [
            header for header in configured if header.lower() not in _APPROVED_CAPTURE_HEADERS
        ]
        if unsafe:
            raise RuntimeError(f"{env_name} contains unsafe telemetry header capture")
    for env_name in _BODY_CAPTURE_ENVS:
        if _truthy(os.getenv(env_name)):
            raise RuntimeError(f"{env_name} is not allowed in hosted telemetry")


def _safe_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int | bool]:
    if not attributes:
        return {}
    safe: dict[str, str | int | bool] = {}
    for key, value in attributes.items():
        normalized_key = str(key)
        if normalized_key not in _ALLOWED_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, bool | int):
            safe[normalized_key] = value
        elif value is not None:
            safe[normalized_key] = _safe_attribute_value(str(value))
    return safe


def _safe_metric_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int | bool]:
    if not attributes:
        return {}
    safe: dict[str, str | int | bool] = {}
    for key, value in attributes.items():
        normalized_key = str(key)
        if normalized_key not in _ALLOWED_METRIC_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, bool | int):
            safe[normalized_key] = value
        elif value is not None:
            safe[normalized_key] = _safe_attribute_value(str(value))
    return safe


def _safe_log_attributes(attributes: Mapping[str, Any] | None) -> dict[str, str | int | bool]:
    if not attributes:
        return {}
    safe: dict[str, str | int | bool] = {}
    for key, value in attributes.items():
        normalized_key = str(key)
        if normalized_key not in _ALLOWED_LOG_ATTRIBUTE_KEYS:
            continue
        if isinstance(value, bool | int):
            safe[normalized_key] = value
        elif value is not None:
            safe[normalized_key] = _safe_attribute_value(str(value))
    return safe


def _safe_attribute_value(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", ".", ":"})[:128]


def _safe_log_event(value: str) -> str:
    return _safe_attribute_value(value) or "event"


def _safe_metric_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "."} else "_" for ch in name)[:128]


def _log_level(value: str) -> int:
    normalized = value.strip().upper()
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(normalized, logging.INFO)


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(part.strip().lower() for part in os.getenv(name, "").split(",") if part.strip())
