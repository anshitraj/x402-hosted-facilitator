from __future__ import annotations

from dataclasses import dataclass

from hosted_facilitator.hosted.tenancy import PaymentProfileConfig, SellerAccountConfig


@dataclass(frozen=True)
class RequestContext:
    trace_id: str
    seller_account: SellerAccountConfig
    payment_profile: PaymentProfileConfig | None = None
    client_host: str | None = None

    @property
    def active_payment_profile(self) -> PaymentProfileConfig:
        return self.payment_profile or self.seller_account.default_payment_profile()
