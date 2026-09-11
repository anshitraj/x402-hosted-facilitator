from __future__ import annotations

from collections.abc import Callable

from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.fingerprint import payment_fingerprint
from hosted_facilitator.hosted.limits import (
    HostedRateLimitEndpoint,
    HostedRateLimiter,
    HostedRateLimitExceededError,
    RateLimitRequest,
)
from hosted_facilitator.hosted.router import ProviderRouter
from hosted_facilitator.hosted.schemas import (
    FacilitatorEnvelope,
    ProviderSettlementStatus,
    SettleOutcome,
    SupportedResponse,
    VerifyOutcome,
)
from hosted_facilitator.hosted.storage import (
    InMemorySettlementStore,
    SettlementAttemptStatus,
    SettlementClaim,
    SettlementRecord,
    SettlementStatus,
    SettlementStore,
)
from hosted_facilitator.hosted.validation import validate_settlement_envelope


class HostedFacilitatorEngine:
    def __init__(
        self,
        *,
        router: ProviderRouter,
        store: SettlementStore | None = None,
        rate_limiter: HostedRateLimiter | None = None,
    ):
        self._router = router
        self._store = store or InMemorySettlementStore()
        self._rate_limiter = rate_limiter

    @property
    def settlement_store(self) -> SettlementStore:
        return self._store

    @property
    def router(self) -> ProviderRouter:
        return self._router

    @property
    def rate_limiter(self) -> HostedRateLimiter | None:
        return self._rate_limiter

    def with_rate_limiter(self, rate_limiter: HostedRateLimiter) -> HostedFacilitatorEngine:
        return HostedFacilitatorEngine(
            router=self._router,
            store=self._store,
            rate_limiter=rate_limiter,
        )

    async def supported(
        self,
        context: RequestContext,
        *,
        provider_allowed: Callable[[str], bool] | None = None,
    ) -> SupportedResponse:
        kinds: list[dict] = []
        payment_profile = context.active_payment_profile
        for provider in self._router.providers_for_payment_profile(payment_profile):
            if provider_allowed is not None and not provider_allowed(provider.name):
                continue
            for kind in await provider.supported(context):
                if _payment_profile_allows_kind(context, provider.name, kind):
                    kinds.append(kind)
        return SupportedResponse(kinds=kinds, traceId=context.trace_id)

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome:
        validate_settlement_envelope(envelope)
        provider = self._router.route(envelope, context.active_payment_profile)
        _validate_provider_envelope(provider, envelope)
        outcome = await provider.verify(envelope, context)
        outcome.trace_id = context.trace_id
        outcome.provider = provider.name
        return outcome

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome:
        validate_settlement_envelope(envelope)
        provider = self._router.route(envelope, context.active_payment_profile)
        _validate_provider_envelope(provider, envelope)
        await _preflight_provider_settlement(provider, envelope, context)
        fingerprint = _settlement_fingerprint(envelope, provider.name)
        requirements = envelope.payment_requirements
        if self._rate_limiter is not None:
            cached = self._cached_existing_settlement(
                context=context,
                provider_name=provider.name,
                scheme=requirements.get("scheme"),
                network=requirements.get("network"),
                fingerprint=fingerprint,
            )
            if cached is not None:
                return cached
        try:
            await self._enforce_settle_submission_rate_limit(context)
        except HostedRateLimitExceededError:
            cached = self._cached_existing_settlement(
                context=context,
                provider_name=provider.name,
                scheme=requirements.get("scheme"),
                network=requirements.get("network"),
                fingerprint=fingerprint,
            )
            if cached is not None:
                return cached
            raise
        record, claim, attempt_id = self._claim_settlement_record(
            envelope,
            context,
            provider.name,
            fingerprint,
        )
        cached = _cached_settlement(record, claim)
        if cached is not None:
            cached.trace_id = context.trace_id
            cached.provider = provider.name
            cached.duplicate = True
            return cached

        if attempt_id is None:
            attempt_id = self._store.start_settle_attempt(record, trace_id=context.trace_id)
        try:
            outcome = await provider.settle(envelope, context)
        except Exception:
            self._mark_record_and_finish_attempt(
                record,
                attempt_id=attempt_id,
                record_status="unknown",
                attempt_status=SettlementAttemptStatus.UNKNOWN,
                transaction=record.transaction,
                payer=record.payer,
                error_reason="provider_settlement_exception",
            )
            return SettleOutcome(
                success=False,
                network=record.network,
                errorReason="settlement_outcome_unknown",
                settlementStatus=ProviderSettlementStatus.UNKNOWN,
                traceId=context.trace_id,
                provider=provider.name,
                duplicate=False,
            )
        outcome.trace_id = context.trace_id
        outcome.provider = provider.name
        outcome.duplicate = False
        if not outcome.payer:
            outcome.payer = _payer_from_payment_payload(envelope.payment_payload)
        settlement_status = _provider_status(outcome)
        outcome.settlement_status = settlement_status
        outcome.success = settlement_status == ProviderSettlementStatus.SETTLED
        if settlement_status == ProviderSettlementStatus.SETTLED:
            self._mark_record_and_finish_attempt(
                record,
                attempt_id=attempt_id,
                record_status="settled",
                attempt_status=SettlementAttemptStatus.SETTLED,
                transaction=outcome.transaction,
                payer=outcome.payer,
                error_reason=None,
            )
        elif settlement_status == ProviderSettlementStatus.SUBMITTED:
            self._mark_record_and_finish_attempt(
                record,
                attempt_id=attempt_id,
                record_status="submitted",
                attempt_status=SettlementAttemptStatus.SUBMITTED,
                transaction=outcome.transaction,
                payer=outcome.payer,
                error_reason=None,
            )
        elif settlement_status == ProviderSettlementStatus.FAILED_FINAL:
            self._mark_record_and_finish_attempt(
                record,
                attempt_id=attempt_id,
                record_status="settle_failed",
                attempt_status=SettlementAttemptStatus.FAILED,
                transaction=record.transaction,
                payer=record.payer,
                error_reason=outcome.error_reason,
            )
        else:
            self._mark_record_and_finish_attempt(
                record,
                attempt_id=attempt_id,
                record_status="unknown",
                attempt_status=SettlementAttemptStatus.UNKNOWN,
                transaction=record.transaction,
                payer=record.payer,
                error_reason=outcome.error_reason,
            )
        return outcome

    def _mark_record_and_finish_attempt(
        self,
        record,
        *,
        attempt_id: int,
        record_status: str,
        attempt_status: SettlementAttemptStatus,
        transaction: str | None,
        payer: str | None,
        error_reason: str | None,
    ) -> None:
        atomic = getattr(self._store, "mark_record_and_finish_settle_attempt", None)
        if atomic is not None:
            atomic(
                record,
                attempt_id=attempt_id,
                record_status=record_status,
                attempt_status=attempt_status,
                transaction=transaction,
                payer=payer,
                error_reason=error_reason,
            )
            return
        if record_status == "settled":
            self._store.mark_settled(record, transaction=transaction, payer=payer)
        elif record_status == "submitted":
            self._store.mark_submitted(record, transaction=transaction, payer=payer)
        elif record_status == "settle_failed":
            self._store.mark_settle_failed(record, error_reason=error_reason)
        else:
            self._store.mark_unknown(record, error_reason=error_reason)
        self._store.finish_settle_attempt(
            attempt_id,
            status=attempt_status,
            transaction=transaction,
            error_reason=error_reason,
        )

    def cached_settlement(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome | None:
        validate_settlement_envelope(envelope)
        provider_name = self._router.provider_name_for_envelope(envelope)
        fingerprint = _settlement_fingerprint(envelope, provider_name)
        requirements = envelope.payment_requirements
        return self._cached_existing_settlement(
            context=context,
            provider_name=provider_name,
            scheme=requirements.get("scheme"),
            network=requirements.get("network"),
            fingerprint=fingerprint,
        )

    def _cached_existing_settlement(
        self,
        *,
        context: RequestContext,
        provider_name: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> SettleOutcome | None:
        existing = self._store.get(
            context.seller_account.seller_account_id,
            fingerprint,
            payment_profile_id=context.active_payment_profile.payment_profile_id,
        )
        if existing is None:
            lookup = getattr(self._store, "get_by_provider_fingerprint", None)
            if callable(lookup):
                existing = lookup(
                    seller_account_id=context.seller_account.seller_account_id,
                    payment_profile_id=context.active_payment_profile.payment_profile_id,
                    provider=provider_name,
                    scheme=scheme,
                    network=network,
                    fingerprint=fingerprint,
                )
        if existing is None:
            return None
        cached = _cached_settlement(existing, _claim_for_existing_status(existing.status))
        if cached is None:
            return None
        cached.trace_id = context.trace_id
        cached.provider = provider_name
        cached.duplicate = True
        return cached

    def _claim_settlement_record(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
        provider_name: str,
        fingerprint: str,
    ) -> tuple[SettlementRecord, SettlementClaim, int | None]:
        requirements = envelope.payment_requirements
        claim_and_start = getattr(self._store, "claim_and_start_settle_attempt", None)
        if claim_and_start is not None:
            return claim_and_start(
                seller_account_id=context.seller_account.seller_account_id,
                payment_profile_id=context.active_payment_profile.payment_profile_id,
                fingerprint=fingerprint,
                provider=provider_name,
                scheme=requirements.get("scheme"),
                network=requirements.get("network"),
                trace_id=context.trace_id,
                raw_requirements=requirements,
            )
        record, claim = self._store.claim_for_settlement(
            seller_account_id=context.seller_account.seller_account_id,
            payment_profile_id=context.active_payment_profile.payment_profile_id,
            fingerprint=fingerprint,
            provider=provider_name,
            scheme=requirements.get("scheme"),
            network=requirements.get("network"),
            trace_id=context.trace_id,
            raw_requirements=requirements,
        )
        return record, claim, None

    async def _enforce_settle_submission_rate_limit(self, context: RequestContext) -> None:
        if self._rate_limiter is None:
            return
        decision = await self._rate_limiter.check(
            RateLimitRequest(
                endpoint=HostedRateLimitEndpoint.SETTLE,
                seller_account_id=context.seller_account.seller_account_id,
                tenant_id=context.seller_account.tenant_id,
                client_host=context.client_host,
                policy=context.active_payment_profile.rate_limits,
            )
        )
        if not decision.allowed:
            raise HostedRateLimitExceededError(decision)


def _validate_provider_envelope(provider: object, envelope: FacilitatorEnvelope) -> None:
    validate_envelope = getattr(provider, "validate_envelope", None)
    if validate_envelope is not None:
        validate_envelope(envelope)


async def _preflight_provider_settlement(
    provider: object,
    envelope: FacilitatorEnvelope,
    context: RequestContext,
) -> None:
    preflight_settlement = getattr(provider, "preflight_settlement", None)
    if preflight_settlement is None:
        return
    result = preflight_settlement(envelope, context)
    if hasattr(result, "__await__"):
        await result


def _settlement_fingerprint(envelope: FacilitatorEnvelope, provider_name: str) -> str:
    return payment_fingerprint(
        payment_payload=envelope.payment_payload,
        payment_requirements=envelope.payment_requirements,
        provider=provider_name,
        x402_version=envelope.x402_version,
    )


def _payer_from_payment_payload(payment_payload: dict) -> str | None:
    payload = payment_payload.get("payload")
    authorization = payload.get("authorization") if isinstance(payload, dict) else None
    if not isinstance(authorization, dict):
        authorization = payment_payload.get("authorization")
    if not isinstance(authorization, dict):
        return None
    payer = authorization.get("from")
    return payer if isinstance(payer, str) and payer else None


def _provider_status(outcome: SettleOutcome) -> ProviderSettlementStatus:
    if outcome.settlement_status is not None:
        return outcome.settlement_status
    if outcome.success:
        return ProviderSettlementStatus.SETTLED
    return ProviderSettlementStatus.UNKNOWN


def _cached_settlement(record: SettlementRecord, claim: SettlementClaim) -> SettleOutcome | None:
    if claim == SettlementClaim.CLAIMED:
        return None
    if record.status == SettlementStatus.SETTLED:
        return SettleOutcome(
            success=True,
            transaction=record.transaction,
            network=record.network,
            payer=record.payer,
            settlementStatus=ProviderSettlementStatus.SETTLED,
        )
    if record.status == SettlementStatus.SETTLE_FAILED:
        return SettleOutcome(
            success=False,
            network=record.network,
            payer=record.payer,
            errorReason=record.error_reason,
            settlementStatus=ProviderSettlementStatus.FAILED_FINAL,
        )
    if record.status == SettlementStatus.SUBMITTED:
        return SettleOutcome(
            success=False,
            transaction=record.transaction,
            network=record.network,
            payer=record.payer,
            errorReason="settlement_submitted",
            settlementStatus=ProviderSettlementStatus.SUBMITTED,
        )
    error_reason = {
        SettlementClaim.DUPLICATE_IN_FLIGHT: "settlement_in_progress",
        SettlementClaim.DUPLICATE_UNKNOWN: "settlement_outcome_unknown",
        SettlementClaim.DUPLICATE_MANUAL_REVIEW: "settlement_manual_review",
    }.get(claim, "duplicate_settlement")
    return SettleOutcome(
        success=False,
        network=record.network,
        payer=record.payer,
        errorReason=error_reason,
        settlementStatus=ProviderSettlementStatus.UNKNOWN,
    )


def _claim_for_existing_status(status: SettlementStatus) -> SettlementClaim:
    if status in {SettlementStatus.SETTLED, SettlementStatus.SETTLE_FAILED}:
        return SettlementClaim.DUPLICATE_FINAL
    if status in {
        SettlementStatus.UNKNOWN,
        SettlementStatus.SUBMITTED,
        SettlementStatus.RECONCILED,
    }:
        return SettlementClaim.DUPLICATE_UNKNOWN
    if status == SettlementStatus.MANUAL_REVIEW:
        return SettlementClaim.DUPLICATE_MANUAL_REVIEW
    return SettlementClaim.DUPLICATE_IN_FLIGHT


def _payment_profile_allows_kind(context: RequestContext, provider_name: str, kind: dict) -> bool:
    asset = kind.get("asset")
    pay_to = kind.get("payTo")
    return context.active_payment_profile.allows(
        provider=provider_name,
        scheme=kind.get("scheme"),
        network=kind.get("network"),
        asset=asset.lower() if isinstance(asset, str) else asset,
        pay_to=pay_to.lower() if isinstance(pay_to, str) else pay_to,
    )
