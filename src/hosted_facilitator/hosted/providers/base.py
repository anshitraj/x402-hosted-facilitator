from __future__ import annotations

from typing import Protocol

from hosted_facilitator.hosted.context import RequestContext
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope, SettleOutcome, VerifyOutcome


class HostedProviderUnavailableError(RuntimeError):
    """Raised before settlement claim when a provider cannot safely submit."""


class HostedFacilitatorProvider(Protocol):
    name: str

    async def supported(self, context: RequestContext) -> list[dict]: ...

    async def verify(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> VerifyOutcome: ...

    async def settle(
        self,
        envelope: FacilitatorEnvelope,
        context: RequestContext,
    ) -> SettleOutcome: ...
