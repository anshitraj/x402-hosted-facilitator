from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from hosted_facilitator.exact import (
    ExactFacilitatorConfig,
    create_exact_facilitator_app,
)
from hosted_facilitator.hosted.auth import (
    AuthenticationError,
    AuthorizationDeniedError,
    AuthorizationUnavailableError,
    OperationsAuthorizer,
    OperationsPermission,
    operations_authorizer_from_env,
)
from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.control_plane import (
    DisabledControlPlaneState,
    InMemoryControlPlaneState,
    register_control_plane_routes,
)
from hosted_facilitator.hosted.engine import HostedFacilitatorEngine
from hosted_facilitator.hosted.limits import (
    DEFAULT_RATE_LIMIT_POLICY,
    HostedRateLimitEndpoint,
    HostedRateLimiter,
    HostedRateLimitExceededError,
    InMemoryFixedWindowRateLimiter,
    RateLimitDecision,
    RateLimitRequest,
)
from hosted_facilitator.hosted.observability import (
    component_health,
)
from hosted_facilitator.hosted.providers.base import HostedProviderUnavailableError
from hosted_facilitator.hosted.providers.exact_evm import HostedExactProvider
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, SupportedResponse
from hosted_facilitator.hosted.signers import HostedSignerGuard
from hosted_facilitator.hosted.storage import InMemorySettlementStore, SettlementStore
from hosted_facilitator.hosted.telemetry import (
    HostedTelemetry,
    HostedTelemetryConfig,
    HostedTelemetryProtocol,
    TelemetryHostedRateLimiter,
)
from hosted_facilitator.hosted.tenancy import (
    PaymentProfileConfig,
    SellerAccountConfig,
    StaticSellerAccountResolver,
)
from hosted_facilitator.hosted.validation import ProtocolValidationError

RATE_LIMIT_DETAIL = "Hosted facilitator rate limit exceeded"
TRUSTED_PROXY_CIDRS_ENV = "OMNICLAW_HOSTED_TRUSTED_PROXY_CIDRS"
INTERNAL_READY_ALLOW_PRIVATE_NETWORKS_ENV = "OMNICLAW_HOSTED_INTERNAL_READY_ALLOW_PRIVATE_NETWORKS"
_SAFE_NETWORK_RE = re.compile(r"^eip155:\d{1,20}$")
_ALLOWED_SCHEMES = {"exact"}


@dataclass(frozen=True)
class _ClientHostResolver:
    trusted_proxy_networks: tuple[Any, ...] = ()

    @classmethod
    def from_config(cls, trusted_proxy_cidrs: tuple[str, ...] | None) -> _ClientHostResolver:
        cidrs = trusted_proxy_cidrs
        if cidrs is None:
            raw = os.getenv(TRUSTED_PROXY_CIDRS_ENV, "")
            cidrs = tuple(part.strip() for part in raw.split(",") if part.strip())
        networks = []
        for cidr in cidrs:
            try:
                networks.append(ip_network(cidr, strict=False))
            except ValueError as exc:
                raise RuntimeError(
                    f"{TRUSTED_PROXY_CIDRS_ENV} contains invalid CIDR: {cidr}"
                ) from exc
        return cls(tuple(networks))

    def resolve(self, request: Request) -> str | None:
        direct_host = request.client.host if request.client else None
        if self._trusts(direct_host):
            cloudflare_client_ip = request.headers.get("cf-connecting-ip", "")
            if cloudflare_client_ip.strip():
                return _safe_client_host(cloudflare_client_ip.strip())
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded.strip():
                return _safe_client_host(forwarded.split(",", 1)[0].strip())
        return _safe_client_host(direct_host)

    def _trusts(self, host: str | None) -> bool:
        if not host:
            return False
        try:
            address = ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_proxy_networks)


def create_hosted_facilitator_app(
    *,
    engine: HostedFacilitatorEngine | None = None,
    resolver: StaticSellerAccountResolver | None = None,
    store_factory: Callable[[], SettlementStore] | None = None,
    rate_limiter: HostedRateLimiter | None = None,
    trusted_proxy_cidrs: tuple[str, ...] | None = None,
    telemetry: HostedTelemetryProtocol | None = None,
    telemetry_config: HostedTelemetryConfig | None = None,
    control_plane_state: Any | None = None,
    operations_authorizer: OperationsAuthorizer | None = None,
) -> FastAPI:
    mode = _hosted_mode()
    if engine is None:
        engine = HostedFacilitatorEngine(router=ProviderRouter(providers=[]))
    rate_limiter = rate_limiter or InMemoryFixedWindowRateLimiter()
    if store_factory is None:
        _assert_settlement_store_safe(engine)
    else:
        _assert_settlement_store_factory_safe(store_factory)
    _assert_rate_limiter_safe(rate_limiter)
    resolver = resolver or StaticSellerAccountResolver()
    client_hosts = _ClientHostResolver.from_config(trusted_proxy_cidrs)
    app = FastAPI(title="OmniClaw Hosted Facilitator")

    @app.middleware("http")
    async def no_store_operations_api(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/ops/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if telemetry is not None and mode not in {"local", "test"}:
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode with injected telemetry; "
            "use validated hosted OpenTelemetry configuration"
        )
    telemetry = telemetry or HostedTelemetry.configure_app(
        app,
        hosted_mode=mode,
        config=telemetry_config,
    )
    _assert_telemetry_safe(telemetry, mode)
    control_plane_state = control_plane_state or _default_control_plane_state(mode)
    _assert_control_plane_state_safe(control_plane_state, mode)
    _assert_seller_account_resolver_safe(resolver, mode)
    app.state.omniclaw_hosted_control_plane_state = control_plane_state
    app.state.omniclaw_hosted_telemetry = telemetry
    rate_limiter = TelemetryHostedRateLimiter(rate_limiter, telemetry)
    engine = engine.with_rate_limiter(rate_limiter)
    _register_settlement_store_lifecycle(app, engine, store_factory)
    _register_rate_limiter_lifecycle(app, rate_limiter)
    _register_telemetry_lifecycle(app, telemetry)
    _register_control_plane_lifecycle(app, control_plane_state)
    _register_seller_account_resolver_lifecycle(app, resolver)

    async def context_from_request(
        request: Request,
        authorization: str | None,
        require_auth: bool = False,
    ) -> RequestContext:
        trace_id = _correlation_id_from_request(request)
        client_host = client_hosts.resolve(request)
        try:
            resolve_access = getattr(resolver, "resolve_access", None)
            if callable(resolve_access):
                access = resolve_access(authorization=authorization, require_auth=require_auth)
                seller_account = access.seller_account
                payment_profile = access.payment_profile
            else:
                seller_account = resolver.resolve(
                    authorization=authorization, require_auth=require_auth
                )
                payment_profile = seller_account.default_payment_profile()
        except KeyError as exc:
            await _penalize_invalid_request(rate_limiter, request, client_hosts=client_hosts)
            raise HTTPException(status_code=404, detail="Unknown seller account") from exc
        except PermissionError as exc:
            await _penalize_invalid_request(rate_limiter, request, client_hosts=client_hosts)
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return RequestContext(
            trace_id=trace_id,
            seller_account=seller_account,
            payment_profile=payment_profile,
            client_host=client_host,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, _exc: RequestValidationError):
        started = time.perf_counter()
        correlation_id = _correlation_id_from_request(request)
        decision = await _consume_invalid_request(rate_limiter, request, client_hosts=client_hosts)
        status_code = 429 if not decision.allowed else 400
        _telemetry_record_counter(
            telemetry,
            "omniclaw.hosted.invalid_requests",
            {
                "endpoint": _endpoint_from_path(request.url.path),
                "reason": "validation",
            },
        )
        _record_http_telemetry(
            telemetry,
            endpoint=_endpoint_from_path(request.url.path),
            status_code=status_code,
            duration_seconds=time.perf_counter() - started,
            correlation_id=correlation_id,
        )
        if not decision.allowed:
            return _rate_limit_json_response(decision)
        return JSONResponse(status_code=400, content={"detail": "Invalid x402 payload"})

    register_routes(
        app,
        engine=engine,
        context_factory=context_from_request,
        store_factory=store_factory,
        rate_limiter=rate_limiter,
        client_hosts=client_hosts,
        telemetry=telemetry,
        control_plane_state=control_plane_state,
    )
    _register_operations_routes(
        app,
        engine=engine,
        store_factory=store_factory,
        resolver=resolver,
        rate_limiter=rate_limiter,
        telemetry=telemetry,
        control_plane_state=control_plane_state,
        operations_authorizer=operations_authorizer,
    )
    return app


def _hosted_mode() -> str:
    raw_mode = os.getenv("OMNICLAW_HOSTED_FACILITATOR_MODE")
    if raw_mode is None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return "test"
        else:
            raise RuntimeError(
                "OMNICLAW_HOSTED_FACILITATOR_MODE must be set to local, test, "
                "or a hosted deployment mode"
            )
    else:
        mode = raw_mode.strip().lower()
        if not mode:
            raise RuntimeError(
                "OMNICLAW_HOSTED_FACILITATOR_MODE must be set to local, test, "
                "or a hosted deployment mode"
            )
    return mode


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_settlement_store_safe(engine: HostedFacilitatorEngine) -> None:
    mode = _hosted_mode()
    if mode in {"local", "test"}:
        return
    if not engine.settlement_store.hosted_safe:
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without an approved "
            "hosted settlement store"
        )


def _assert_settlement_store_factory_safe(store_factory: Callable[[], SettlementStore]) -> None:
    mode = _hosted_mode()
    if mode in {"local", "test"}:
        return
    store = store_factory()
    try:
        if not store.hosted_safe:
            raise RuntimeError(
                "Hosted facilitator cannot run outside local/test mode without an approved "
                "hosted settlement store factory"
            )
    finally:
        _close_store(store)


def _assert_rate_limiter_safe(rate_limiter: HostedRateLimiter) -> None:
    mode = _hosted_mode()
    if mode in {"local", "test"}:
        return
    if not rate_limiter.hosted_safe:
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without an approved "
            "hosted rate limiter"
        )


def _assert_telemetry_safe(telemetry: HostedTelemetryProtocol, mode: str) -> None:
    if mode in {"local", "test"}:
        return
    if not telemetry.hosted_safe:
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without approved "
            "hosted OpenTelemetry"
        )


def _default_control_plane_state(mode: str) -> Any:
    if mode in {"local", "test"}:
        return InMemoryControlPlaneState()
    return DisabledControlPlaneState()


def _assert_control_plane_state_safe(control_plane_state: Any, mode: str) -> None:
    if mode in {"local", "test"}:
        return
    if not getattr(control_plane_state, "hosted_safe", False):
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without approved "
            "hosted control-plane state"
        )
    if getattr(control_plane_state, "writes_enabled", False) and not getattr(
        control_plane_state, "durable", False
    ):
        raise RuntimeError(
            "Hosted facilitator cannot enable control-plane writes without durable "
            "control-plane state"
        )


def _assert_seller_account_resolver_safe(resolver: Any, mode: str) -> None:
    if mode in {"local", "test"}:
        return
    if not getattr(resolver, "hosted_safe", False):
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without an approved "
            "hosted seller account resolver"
        )


def _assert_hosted_exact_signer_guard_safe(
    signer_guard: HostedSignerGuard | None, mode: str
) -> None:
    if mode in {"local", "test"}:
        return
    if signer_guard is None:
        raise RuntimeError("Hosted exact settlement requires an approved signer guard")
    if signer_guard.config.min_native_balance_wei is None:
        raise RuntimeError("Hosted exact settlement requires a signer gas floor")


def create_hosted_exact_facilitator_app(
    config: ExactFacilitatorConfig,
    *,
    network_configs: tuple[ExactFacilitatorConfig, ...] | None = None,
    seller_accounts: list[SellerAccountConfig] | None = None,
    resolver: Any | None = None,
    store: SettlementStore | None = None,
    store_factory: Callable[[], SettlementStore] | None = None,
    rate_limiter: HostedRateLimiter | None = None,
    trusted_proxy_cidrs: tuple[str, ...] | None = None,
    telemetry: HostedTelemetryProtocol | None = None,
    telemetry_config: HostedTelemetryConfig | None = None,
    control_plane_state: Any | None = None,
    operations_authorizer: OperationsAuthorizer | None = None,
    signer_guard: HostedSignerGuard | None = None,
    extra_providers: tuple[Any, ...] = (),
    provider_timeout_seconds: float = 30.0,
    max_concurrent_settlements: int = 1,
    **exact_app_kwargs,
) -> FastAPI:
    """Create hosted control-plane routes backed by OmniClaw exact settlement."""
    mode = _hosted_mode()
    _assert_hosted_exact_signer_guard_safe(signer_guard, mode)
    provider, exact_facilitators = _build_hosted_exact_provider(
        config,
        network_configs=network_configs,
        signer_guard=signer_guard,
        provider_timeout_seconds=provider_timeout_seconds,
        max_concurrent_settlements=max_concurrent_settlements,
        exact_app_kwargs=exact_app_kwargs,
    )
    engine = HostedFacilitatorEngine(
        router=ProviderRouter([provider, *extra_providers]),
        store=store or InMemorySettlementStore(),
    )
    resolver = resolver or StaticSellerAccountResolver(seller_accounts)
    app = create_hosted_facilitator_app(
        engine=engine,
        resolver=resolver,
        store_factory=store_factory,
        rate_limiter=rate_limiter,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        telemetry=telemetry,
        telemetry_config=telemetry_config,
        control_plane_state=control_plane_state,
        operations_authorizer=operations_authorizer,
    )
    app.state.omniclaw_exact_facilitator_config = {
        "host": config.host,
        "port": config.port,
        "rpc_url": config.rpc_url,
        "networks": config.networks,
        "network_profile": config.network_profile,
        "title": config.title,
    }
    app.state.omniclaw_exact_facilitator = next(iter(exact_facilitators.values()))
    app.state.omniclaw_exact_facilitators_by_network = exact_facilitators
    app.state.omniclaw_hosted_providers = {
        provider.name: provider,
        **{extra_provider.name: extra_provider for extra_provider in extra_providers},
    }
    return app


def _build_hosted_exact_provider(
    config: ExactFacilitatorConfig,
    *,
    network_configs: tuple[ExactFacilitatorConfig, ...] | None,
    signer_guard: HostedSignerGuard | None,
    provider_timeout_seconds: float,
    max_concurrent_settlements: int,
    exact_app_kwargs: dict[str, Any],
) -> tuple[HostedExactProvider, dict[str, Any]]:
    configs = network_configs or (config,)
    _validate_hosted_exact_network_configs(configs)
    exact_facilitators: dict[str, Any] = {}
    for network_config in configs:
        exact_app = create_exact_facilitator_app(network_config, **exact_app_kwargs)
        facilitator = exact_app.state.omniclaw_exact_facilitator
        for network in network_config.networks:
            exact_facilitators[network] = facilitator
    return (
        HostedExactProvider(
            exact_facilitators,
            signer_guard=signer_guard,
            timeout_seconds=provider_timeout_seconds,
            max_concurrent_settlements=max_concurrent_settlements,
        ),
        exact_facilitators,
    )


def _validate_hosted_exact_network_configs(configs: tuple[ExactFacilitatorConfig, ...]) -> None:
    if not configs:
        raise RuntimeError("At least one hosted exact network config is required")
    shared_private_key = configs[0].private_key
    seen_networks: set[str] = set()
    for network_config in configs:
        if network_config.private_key != shared_private_key:
            raise RuntimeError("Hosted exact network configs must use one shared EVM signer key")
        if len(network_config.networks) != 1:
            raise RuntimeError("Hosted exact network configs must contain exactly one network each")
        network = network_config.networks[0]
        if network in seen_networks:
            raise RuntimeError(f"Duplicate hosted exact network config: {network}")
        seen_networks.add(network)


def register_routes(
    app: FastAPI,
    *,
    engine: HostedFacilitatorEngine,
    context_factory: Callable,
    store_factory: Callable[[], SettlementStore] | None = None,
    rate_limiter: HostedRateLimiter,
    client_hosts: _ClientHostResolver,
    telemetry: HostedTelemetryProtocol,
    control_plane_state: Any,
) -> None:
    supported_paths = [
        "/supported",
        "/v1/x402/supported",
        "/v2/x402/supported",
    ]
    verify_paths = [
        "/verify",
        "/v1/x402/verify",
        "/v2/x402/verify",
    ]
    settle_paths = [
        "/settle",
        "/v1/x402/settle",
        "/v2/x402/settle",
    ]

    async def supported(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        started = time.perf_counter()
        status_code = 500
        endpoint = HostedRateLimitEndpoint.SUPPORTED.value
        correlation_id = _correlation_id_from_request(request)
        seller_account_id: str | None = None
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.supported",
            {"omniclaw.endpoint": endpoint, "omniclaw.correlation_id": correlation_id},
        ) as span:
            try:
                context = await context_factory(request, authorization, False)
                seller_account_id = context.seller_account.seller_account_id
                await _enforce_rate_limit(rate_limiter, context, HostedRateLimitEndpoint.SUPPORTED)
                if _control_plane_blocks_supported(
                    context.active_payment_profile, control_plane_state
                ):
                    result = _empty_supported_response(context.trace_id)
                else:
                    result = await engine.supported(
                        context,
                        provider_allowed=lambda provider: _control_plane_allows_provider(
                            control_plane_state,
                            provider,
                        ),
                    )
                    result.kinds = [
                        kind
                        for kind in result.kinds
                        if _control_plane_allows_kind(engine, control_plane_state, kind)
                    ]
                status_code = 200
                _telemetry_set_attributes(telemetry, span, {"omniclaw.status": "ok"})
                return result.model_dump(by_alias=True, exclude_none=True)
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _record_http_telemetry(
                    telemetry,
                    endpoint=endpoint,
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                    seller_account_id=seller_account_id,
                )

    async def verify(
        envelope: FacilitatorEnvelope,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        started = time.perf_counter()
        status_code = 500
        endpoint = HostedRateLimitEndpoint.VERIFY.value
        provider = "unknown"
        network = "unknown"
        correlation_id = _correlation_id_from_request(request)
        seller_account_id: str | None = None
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.verify",
            {
                "omniclaw.endpoint": endpoint,
                "omniclaw.correlation_id": correlation_id,
                "omniclaw.network": network,
                "omniclaw.scheme": _safe_requirement_scheme(envelope.payment_requirements),
            },
        ) as span:
            try:
                if envelope.x402_version != 2:
                    await _penalize_invalid_request(
                        rate_limiter, request, client_hosts=client_hosts
                    )
                    raise HTTPException(status_code=400, detail="Only x402Version=2 is supported")
                context = await context_factory(request, authorization, True)
                seller_account_id = context.seller_account.seller_account_id
                await _enforce_rate_limit(rate_limiter, context, HostedRateLimitEndpoint.VERIFY)
                _enforce_control_plane_route(engine, control_plane_state, envelope)
                try:
                    result = await engine.verify(envelope, context)
                except ProtocolValidationError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.invalid_requests",
                        {"endpoint": endpoint, "reason": "protocol_validation"},
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except ValidationError:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.invalid_requests",
                        {"endpoint": endpoint, "reason": "payload_validation"},
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(status_code=400, detail="Invalid x402 payload") from None
                except HostedProviderUnavailableError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.provider_unavailable",
                        {
                            "endpoint": endpoint,
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    raise HTTPException(status_code=503, detail="Provider unavailable") from exc
                except PermissionError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.policy_denied",
                        {
                            "endpoint": endpoint,
                            "provider": provider,
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(
                        status_code=403, detail="Seller policy denied route"
                    ) from exc
                except LookupError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.unsupported_route",
                        {
                            "endpoint": endpoint,
                            "scheme": _safe_requirement_scheme(envelope.payment_requirements),
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(
                        status_code=400, detail="Unsupported payment route"
                    ) from exc
                status_code = 200
                provider = _safe_provider(result.provider)
                network = _safe_requirement_network(envelope.payment_requirements)
                status = "valid" if result.is_valid else "invalid"
                _telemetry_set_attributes(
                    telemetry,
                    span,
                    {
                        "omniclaw.provider": provider,
                        "omniclaw.network": network,
                        "omniclaw.status": status,
                    },
                )
                _telemetry_record_counter(
                    telemetry,
                    "omniclaw.hosted.verify.requests",
                    {
                        "provider": provider,
                        "network": network,
                        "status": status,
                    },
                )
                _telemetry_record_histogram(
                    telemetry,
                    "omniclaw.hosted.verify.duration",
                    time.perf_counter() - started,
                    {
                        "provider": provider,
                        "network": network,
                        "status": status,
                    },
                )
                return result.model_dump(by_alias=True, exclude_none=True)
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _record_http_telemetry(
                    telemetry,
                    endpoint=endpoint,
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                    seller_account_id=seller_account_id,
                    provider=provider,
                    network=network,
                )

    async def settle(
        envelope: FacilitatorEnvelope,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        started = time.perf_counter()
        status_code = 500
        endpoint = HostedRateLimitEndpoint.SETTLE.value
        provider = "unknown"
        network = "unknown"
        correlation_id = _correlation_id_from_request(request)
        seller_account_id: str | None = None
        with _telemetry_span(
            telemetry,
            "omniclaw.hosted.settle",
            {
                "omniclaw.endpoint": endpoint,
                "omniclaw.correlation_id": correlation_id,
                "omniclaw.network": network,
                "omniclaw.scheme": _safe_requirement_scheme(envelope.payment_requirements),
            },
        ) as span:
            try:
                if envelope.x402_version != 2:
                    await _penalize_invalid_request(
                        rate_limiter, request, client_hosts=client_hosts
                    )
                    raise HTTPException(status_code=400, detail="Only x402Version=2 is supported")
                context = await context_factory(request, authorization, True)
                seller_account_id = context.seller_account.seller_account_id
                request_engine, request_store = _settlement_engine_for_request(
                    engine,
                    store_factory,
                )
                try:
                    _enforce_control_plane_route(engine, control_plane_state, envelope)
                    result = await request_engine.settle(envelope, context)
                except ProtocolValidationError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.invalid_requests",
                        {"endpoint": endpoint, "reason": "protocol_validation"},
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                except ValidationError:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.invalid_requests",
                        {"endpoint": endpoint, "reason": "payload_validation"},
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(status_code=400, detail="Invalid x402 payload") from None
                except HostedRateLimitExceededError as exc:
                    raise HTTPException(
                        status_code=429,
                        detail=RATE_LIMIT_DETAIL,
                        headers=exc.decision.headers(),
                    ) from exc
                except HostedProviderUnavailableError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.provider_unavailable",
                        {
                            "endpoint": endpoint,
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    raise HTTPException(status_code=503, detail="Provider unavailable") from exc
                except PermissionError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.policy_denied",
                        {
                            "endpoint": endpoint,
                            "provider": provider,
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(
                        status_code=403, detail="Seller policy denied route"
                    ) from exc
                except LookupError as exc:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.unsupported_route",
                        {
                            "endpoint": endpoint,
                            "scheme": _safe_requirement_scheme(envelope.payment_requirements),
                            "network": _safe_requirement_network(envelope.payment_requirements),
                        },
                    )
                    await _penalize_invalid_request(
                        rate_limiter,
                        request,
                        context.seller_account,
                        payment_profile=context.active_payment_profile,
                        client_hosts=client_hosts,
                    )
                    raise HTTPException(
                        status_code=400, detail="Unsupported payment route"
                    ) from exc
                finally:
                    if request_store is not None:
                        _close_store(request_store)
                status_code = 200
                provider = _safe_provider(result.provider)
                network = _safe_network(result.network) or _safe_requirement_network(
                    envelope.payment_requirements
                )
                status = (
                    result.settlement_status.value
                    if result.settlement_status is not None
                    else ("success" if result.success else "failed")
                )
                duplicate = bool(result.duplicate)
                _telemetry_set_attributes(
                    telemetry,
                    span,
                    {
                        "omniclaw.provider": provider,
                        "omniclaw.network": network,
                        "omniclaw.status": status,
                        "omniclaw.duplicate": duplicate,
                    },
                )
                _telemetry_record_counter(
                    telemetry,
                    "omniclaw.hosted.settle.requests",
                    {
                        "provider": provider,
                        "network": network,
                        "status": status,
                        "duplicate": duplicate,
                    },
                )
                _telemetry_record_histogram(
                    telemetry,
                    "omniclaw.hosted.settle.duration",
                    time.perf_counter() - started,
                    {
                        "provider": provider,
                        "network": network,
                        "status": status,
                    },
                )
                if duplicate:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.settle.duplicates",
                        {
                            "provider": provider,
                            "network": network,
                            "status": status,
                        },
                    )
                if status == "unknown" and not duplicate:
                    _telemetry_record_counter(
                        telemetry,
                        "omniclaw.hosted.settle.unknown_outcomes",
                        {
                            "provider": provider,
                            "network": network,
                        },
                    )
                return result.model_dump(by_alias=True, exclude_none=True)
            except HTTPException as exc:
                status_code = exc.status_code
                raise
            finally:
                _record_http_telemetry(
                    telemetry,
                    endpoint=endpoint,
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - started,
                    correlation_id=correlation_id,
                    seller_account_id=seller_account_id,
                    provider=provider,
                    network=network,
                )

    for path in supported_paths:
        app.add_api_route(path, supported, methods=["GET"])
    for path in verify_paths:
        app.add_api_route(path, verify, methods=["POST"])
    for path in settle_paths:
        app.add_api_route(path, settle, methods=["POST"])


def _register_operations_routes(
    app: FastAPI,
    *,
    engine: HostedFacilitatorEngine,
    store_factory: Callable[[], SettlementStore] | None,
    resolver: Any,
    rate_limiter: HostedRateLimiter,
    telemetry: HostedTelemetryProtocol,
    control_plane_state: Any,
    operations_authorizer: OperationsAuthorizer | None,
) -> None:
    authorizer = operations_authorizer or operations_authorizer_from_env(
        hosted_mode=_hosted_mode(),
    )
    _assert_operations_authorizer_safe(authorizer)

    async def health():
        return {"status": "ok", "service": "omniclaw_hosted_facilitator"}

    async def readiness_payload():
        store = None
        close_store = False
        try:
            store = store_factory() if store_factory is not None else engine.settlement_store
            close_store = store_factory is not None
            store_health = await component_health("settlement_store", store)
        except Exception as exc:
            store_health = {
                "name": "settlement_store",
                "status": "unhealthy",
                "errorType": type(exc).__name__,
            }
        finally:
            if close_store and store is not None:
                _close_store(store)
        rate_limiter_health = await component_health("rate_limiter", rate_limiter)
        telemetry_health = await component_health("telemetry", telemetry)
        control_plane_health = await component_health("control_plane", control_plane_state)
        seller_account_resolver_health = await component_health("seller_account_resolver", resolver)
        provider_names = engine.router.provider_names()
        provider_count = len(provider_names)
        provider_checks = []
        for provider_name in provider_names:
            provider = engine.router.provider_by_name(provider_name)
            if provider is not None:
                provider_checks.append(await component_health(provider_name, provider))
        providers_health = {
            "name": "providers",
            "status": (
                "ok"
                if provider_count > 0
                and all(provider["status"] != "unhealthy" for provider in provider_checks)
                else "unhealthy"
            ),
            "providerCount": provider_count,
            "items": provider_checks,
        }
        components = {
            "settlementStore": store_health,
            "rateLimiter": rate_limiter_health,
            "telemetry": telemetry_health,
            "controlPlane": control_plane_health,
            "sellerAccountResolver": seller_account_resolver_health,
            "providers": providers_health,
        }
        status = (
            "ok"
            if all(component["status"] == "ok" for component in components.values())
            else "unhealthy"
        )
        content = {"status": status, "components": components}
        if status != "ok":
            return JSONResponse(status_code=503, content=content)
        return content

    async def ready(request: Request):
        await _authorize_operations_request(
            request,
            authorizer=authorizer,
            permission=OperationsPermission.OPS_OVERVIEW,
        )
        return await readiness_payload()

    async def internal_ready(request: Request):
        client_host = request.client.host if request.client is not None else ""
        if client_host == "testclient":
            return await readiness_payload()
        try:
            client_ip = ip_address(client_host)
            private_network_allowed = _env_truthy(INTERNAL_READY_ALLOW_PRIVATE_NETWORKS_ENV)
            if not client_ip.is_loopback and not (private_network_allowed and client_ip.is_private):
                raise HTTPException(status_code=404, detail="Not found")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return await readiness_payload()

    for path in ("/health", "/healthz"):
        app.add_api_route(path, health, methods=["GET"], include_in_schema=False)
    for path in ("/ready", "/readyz"):
        app.add_api_route(path, ready, methods=["GET"], include_in_schema=False)
    app.add_api_route("/internal/readyz", internal_ready, methods=["GET"], include_in_schema=False)
    register_control_plane_routes(
        app,
        engine=engine,
        store_factory=store_factory,
        rate_limiter=rate_limiter,
        telemetry=telemetry,
        control_state=control_plane_state,
        seller_account_resolver=resolver,
        authorize_request=lambda request, permission: _authorize_operations_request(
            request,
            authorizer=authorizer,
            permission=permission,
        ),
    )


async def _authorize_operations_request(
    request: Request,
    *,
    authorizer: OperationsAuthorizer,
    permission: OperationsPermission,
):
    try:
        principal = await authorizer.authorize(request.headers.get("authorization"), permission)
    except AuthorizationUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail="Hosted operations authorization unavailable"
        ) from exc
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail="Unauthorized") from exc
    except AuthorizationDeniedError as exc:
        raise HTTPException(status_code=403, detail="Forbidden") from exc
    if not (principal.issuer == "local" and principal.subject == "local-operator"):
        request.state.omniclaw_principal = principal
    return principal


def _assert_operations_authorizer_safe(authorizer: OperationsAuthorizer) -> None:
    mode = _hosted_mode()
    if mode in {"local", "test"}:
        return
    if not getattr(authorizer, "hosted_safe", False):
        raise RuntimeError(
            "Hosted facilitator cannot run outside local/test mode without an approved "
            "hosted operations authorizer"
        )


def _endpoint_from_path(path: str) -> str:
    if path.endswith("/supported"):
        return HostedRateLimitEndpoint.SUPPORTED.value
    if path.endswith("/verify"):
        return HostedRateLimitEndpoint.VERIFY.value
    if path.endswith("/settle"):
        return HostedRateLimitEndpoint.SETTLE.value
    return "unknown"


def _record_http_telemetry(
    telemetry: HostedTelemetryProtocol,
    *,
    endpoint: str,
    status_code: int,
    duration_seconds: float,
    correlation_id: str | None = None,
    seller_account_id: str | None = None,
    provider: str | None = None,
    network: str | None = None,
) -> None:
    attributes = {"endpoint": endpoint, "status": str(status_code)}
    _telemetry_record_counter(telemetry, "omniclaw.hosted.http.server.requests", attributes)
    _telemetry_record_histogram(
        telemetry,
        "omniclaw.hosted.http.server.duration",
        duration_seconds,
        attributes,
    )
    log_attributes: dict[str, Any] = {
        "endpoint": endpoint,
        "status": str(status_code),
        "http.status_code": status_code,
        "duration_ms": int(duration_seconds * 1000),
    }
    if correlation_id:
        log_attributes["omniclaw.correlation_id"] = correlation_id
    if seller_account_id:
        log_attributes["omniclaw.seller_account_id"] = seller_account_id
    if provider:
        log_attributes["provider"] = provider
    if network:
        log_attributes["network"] = network
    _telemetry_log_event(telemetry, "info", "http_request", log_attributes)


@contextmanager
def _telemetry_span(
    telemetry: HostedTelemetryProtocol,
    name: str,
    attributes: dict[str, Any] | None = None,
):
    manager = None
    span = None
    try:
        manager = telemetry.span(name, attributes)
        span = manager.__enter__()
    except Exception:
        yield None
        return
    try:
        yield span
    except BaseException:
        exc_info = sys.exc_info()
        with suppress(Exception):
            manager.__exit__(*exc_info)
        raise
    else:
        with suppress(Exception):
            manager.__exit__(None, None, None)


def _telemetry_set_attributes(
    telemetry: HostedTelemetryProtocol,
    span: Any,
    attributes: dict[str, Any] | None,
) -> None:
    try:
        telemetry.set_attributes(span, attributes)
    except Exception:
        return


def _telemetry_record_counter(
    telemetry: HostedTelemetryProtocol,
    name: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    try:
        telemetry.record_counter(name, attributes)
    except Exception:
        return


def _telemetry_record_histogram(
    telemetry: HostedTelemetryProtocol,
    name: str,
    value: float,
    attributes: dict[str, Any] | None = None,
) -> None:
    try:
        telemetry.record_histogram(name, value, attributes)
    except Exception:
        return


def _telemetry_log_event(
    telemetry: HostedTelemetryProtocol,
    level: str,
    event: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    try:
        telemetry.log_event(level, event, attributes)
    except Exception:
        return


def _safe_requirement_network(requirements: dict[str, Any]) -> str:
    return _safe_network(requirements.get("network")) or "unknown"


def _safe_requirement_scheme(requirements: dict[str, Any]) -> str:
    value = requirements.get("scheme")
    if isinstance(value, str) and value in _ALLOWED_SCHEMES:
        return value
    return "unknown"


def _safe_network(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_NETWORK_RE.fullmatch(value):
        return value
    return None


def _safe_provider(value: str | None) -> str:
    if value:
        safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", "."})[:64]
        if safe:
            return safe
    return "unknown"


def _control_plane_allows_kind(
    engine: HostedFacilitatorEngine,
    control_plane_state: Any,
    kind: dict[str, Any],
) -> bool:
    allows = getattr(control_plane_state, "allows", None)
    if allows is None:
        return True
    provider = engine.router.provider_name_for_kind(kind)
    network = kind.get("network") if isinstance(kind.get("network"), str) else None
    return bool(allows(provider=provider, network=network))


def _control_plane_allows_provider(control_plane_state: Any, provider: str) -> bool:
    allows = getattr(control_plane_state, "allows", None)
    if allows is None:
        return True
    return bool(allows(provider=provider, network=None))


def _control_plane_blocks_supported(
    payment_profile: PaymentProfileConfig, control_plane_state: Any
) -> bool:
    allows = getattr(control_plane_state, "allows", None)
    if allows is None:
        return False
    if not any(
        bool(allows(provider=provider, network=None))
        for provider in payment_profile.enabled_providers
    ):
        return True
    return not any(
        bool(allows(provider="network_probe", network=network))
        for network in payment_profile.enabled_networks
    )


def _empty_supported_response(trace_id: str) -> SupportedResponse:
    return SupportedResponse(kinds=[], traceId=trace_id)


def _enforce_control_plane_route(
    engine: HostedFacilitatorEngine,
    control_plane_state: Any,
    envelope: FacilitatorEnvelope,
) -> None:
    allows = getattr(control_plane_state, "allows", None)
    if allows is None:
        return
    provider = engine.router.provider_name_for_envelope(envelope)
    network = (
        envelope.payment_requirements.get("network")
        if isinstance(envelope.payment_requirements.get("network"), str)
        else None
    )
    if not allows(provider=provider, network=network):
        raise HTTPException(status_code=423, detail="Hosted facilitator route is paused")


def _trace_id(value: str | None) -> str:
    if not value:
        return f"tr_{uuid4().hex}"
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-", "."})
    if not safe:
        return f"tr_{uuid4().hex}"
    return safe[:96]


def _correlation_id_from_request(request: Request) -> str:
    existing = getattr(request.state, "omniclaw_correlation_id", None)
    if existing:
        return existing
    correlation_id = _trace_id(request.headers.get("x-request-id"))
    request.state.omniclaw_correlation_id = correlation_id
    return correlation_id


def _register_rate_limiter_lifecycle(app: FastAPI, rate_limiter: HostedRateLimiter) -> None:
    initialize = getattr(rate_limiter, "initialize", None)
    if initialize is not None:
        app.router.add_event_handler("startup", initialize)
    close = getattr(rate_limiter, "close", None)
    if close is not None:
        app.router.add_event_handler("shutdown", close)


def _register_settlement_store_lifecycle(
    app: FastAPI,
    engine: HostedFacilitatorEngine,
    store_factory: Callable[[], SettlementStore] | None,
) -> None:
    if store_factory is not None:

        def initialize_factory_store() -> None:
            store = store_factory()
            try:
                initialize = getattr(store, "initialize", None)
                if initialize is not None:
                    initialize()
            finally:
                _close_store(store)

        app.router.add_event_handler("startup", initialize_factory_store)
        close_factory = getattr(store_factory, "close", None)
        if close_factory is not None:
            app.router.add_event_handler("shutdown", close_factory)
        return
    store = engine.settlement_store
    initialize = getattr(store, "initialize", None)
    if initialize is not None:
        app.router.add_event_handler("startup", initialize)
    close = getattr(store, "close", None)
    if close is not None:
        app.router.add_event_handler("shutdown", close)


def _register_telemetry_lifecycle(app: FastAPI, telemetry: HostedTelemetryProtocol) -> None:
    close = getattr(telemetry, "close", None)
    if close is not None:
        app.router.add_event_handler("shutdown", close)


def _register_control_plane_lifecycle(app: FastAPI, control_plane_state: Any) -> None:
    initialize = getattr(control_plane_state, "initialize", None)
    if initialize is not None:
        app.router.add_event_handler("startup", initialize)
    close = getattr(control_plane_state, "close", None)
    if close is not None:
        app.router.add_event_handler("shutdown", close)


def _register_seller_account_resolver_lifecycle(app: FastAPI, resolver: Any) -> None:
    initialize = getattr(resolver, "initialize", None)
    if initialize is not None:
        app.router.add_event_handler("startup", initialize)
    close = getattr(resolver, "close", None)
    if close is not None:
        app.router.add_event_handler("shutdown", close)


def _safe_client_host(host: str | None) -> str | None:
    if not host:
        return None
    return host[:128]


async def _enforce_rate_limit(
    rate_limiter: HostedRateLimiter,
    context: RequestContext,
    endpoint: HostedRateLimitEndpoint,
) -> None:
    decision = await rate_limiter.check(
        RateLimitRequest(
            endpoint=endpoint,
            seller_account_id=context.seller_account.seller_account_id,
            tenant_id=context.seller_account.tenant_id,
            client_host=context.client_host,
            policy=context.active_payment_profile.rate_limits,
        )
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=RATE_LIMIT_DETAIL,
            headers=decision.headers(),
        )


async def _penalize_invalid_request(
    rate_limiter: HostedRateLimiter,
    request: Request,
    seller_account: SellerAccountConfig | None = None,
    payment_profile: PaymentProfileConfig | None = None,
    *,
    client_hosts: _ClientHostResolver,
) -> None:
    decision = await _consume_invalid_request(
        rate_limiter,
        request,
        seller_account,
        payment_profile=payment_profile,
        client_hosts=client_hosts,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=RATE_LIMIT_DETAIL,
            headers=decision.headers(),
        )


async def _consume_invalid_request(
    rate_limiter: HostedRateLimiter,
    request: Request,
    seller_account: SellerAccountConfig | None = None,
    payment_profile: PaymentProfileConfig | None = None,
    *,
    client_hosts: _ClientHostResolver,
) -> RateLimitDecision:
    policy = (
        payment_profile.rate_limits
        if payment_profile is not None
        else seller_account.rate_limits
        if seller_account is not None
        else DEFAULT_RATE_LIMIT_POLICY
    )
    return await rate_limiter.check(
        RateLimitRequest(
            endpoint=HostedRateLimitEndpoint.INVALID,
            seller_account_id=seller_account.seller_account_id if seller_account else None,
            tenant_id=seller_account.tenant_id if seller_account else None,
            client_host=client_hosts.resolve(request),
            policy=policy,
        )
    )


def _rate_limit_json_response(decision: RateLimitDecision) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": RATE_LIMIT_DETAIL},
        headers=decision.headers(),
    )


def _settlement_engine_for_request(
    engine: HostedFacilitatorEngine,
    store_factory: Callable[[], SettlementStore] | None,
) -> tuple[HostedFacilitatorEngine, SettlementStore | None]:
    if store_factory is None:
        return engine, None
    store = store_factory()
    return HostedFacilitatorEngine(
        router=engine.router,
        store=store,
        rate_limiter=engine.rate_limiter,
    ), store


def _close_store(store: SettlementStore) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        close()
