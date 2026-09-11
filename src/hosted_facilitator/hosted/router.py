from __future__ import annotations

from hosted_facilitator.hosted.providers.base import HostedFacilitatorProvider
from hosted_facilitator.hosted.schemas import FacilitatorEnvelope
from hosted_facilitator.hosted.tenancy import PaymentProfileConfig, SellerAccountConfig


class ProviderRouter:
    def __init__(self, providers: list[HostedFacilitatorProvider]):
        self._providers = {provider.name: provider for provider in providers}

    def route(
        self, envelope: FacilitatorEnvelope, payment_profile: PaymentProfileConfig
    ) -> HostedFacilitatorProvider:
        requirements = envelope.payment_requirements
        scheme = requirements.get("scheme")
        network = requirements.get("network")
        asset = requirements.get("asset")
        pay_to = requirements.get("payTo")
        extra = requirements.get("extra") if isinstance(requirements.get("extra"), dict) else {}

        provider_name = self._provider_name_for(scheme=scheme, network=network, extra=extra)
        provider = self._providers.get(provider_name)
        if provider is None:
            raise LookupError(f"No hosted facilitator provider registered for {provider_name}")
        if not payment_profile.allows(
            scheme=scheme,
            network=network,
            provider=provider_name,
            asset=asset.lower() if isinstance(asset, str) else asset,
            pay_to=pay_to.lower() if isinstance(pay_to, str) else pay_to,
        ):
            raise PermissionError(
                f"Payment profile {payment_profile.payment_profile_id} does not allow {provider_name} "
                f"for {scheme or 'unknown'} on {network or 'unknown'}"
            )
        return provider

    def provider_name_for_envelope(self, envelope: FacilitatorEnvelope) -> str:
        requirements = envelope.payment_requirements
        extra = requirements.get("extra") if isinstance(requirements.get("extra"), dict) else {}
        return self._provider_name_for(
            scheme=requirements.get("scheme"),
            network=requirements.get("network"),
            extra=extra,
        )

    def provider_name_for_kind(self, kind: dict) -> str:
        extra = kind.get("extra") if isinstance(kind.get("extra"), dict) else {}
        return self._provider_name_for(
            scheme=kind.get("scheme"),
            network=kind.get("network"),
            extra=extra,
        )

    def providers_for_payment_profile(
        self, payment_profile: PaymentProfileConfig
    ) -> list[HostedFacilitatorProvider]:
        return [
            provider
            for provider in self._providers.values()
            if provider.name in payment_profile.enabled_providers
        ]

    def providers_for_seller_account(
        self, seller_account: SellerAccountConfig
    ) -> list[HostedFacilitatorProvider]:
        return self.providers_for_payment_profile(seller_account.default_payment_profile())

    def provider_by_name(self, provider_name: str) -> HostedFacilitatorProvider | None:
        return self._providers.get(provider_name)

    def provider_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def _provider_name_for(
        self,
        *,
        scheme: str | None,
        network: str | None,
        extra: dict,
    ) -> str:
        for provider in self._providers.values():
            matches = getattr(provider, "matches", None)
            if matches is not None and matches(scheme=scheme, network=network, extra=extra):
                return provider.name
        exact_evm = self._providers.get("exact_evm")
        if (
            scheme == "exact"
            and exact_evm is not None
            and _provider_supports_network(exact_evm, network)
        ):
            return "exact_evm"
        if scheme == "exact":
            return "delegated_http"
        return "unsupported"


def _provider_supports_network(provider: HostedFacilitatorProvider, network: str | None) -> bool:
    supported_networks = getattr(provider, "supported_networks", ())
    if not supported_networks:
        return True
    return network in supported_networks
