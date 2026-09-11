from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass, field
from hmac import compare_digest

from hosted_facilitator.hosted.limits import DEFAULT_RATE_LIMIT_POLICY, RateLimitPolicy

API_KEY_PREFIX = "omck"


@dataclass(frozen=True)
class PaymentProfileConfig:
    payment_profile_id: str = "default"
    seller_account_id: str = "default"
    name: str = "Default"
    status: str = "active"
    enabled_networks: tuple[str, ...] = ("eip155:5042002",)
    enabled_schemes: tuple[str, ...] = ("exact",)
    enabled_providers: tuple[str, ...] = ("exact_evm",)
    api_keys: tuple[str, ...] = field(default_factory=tuple)
    allowed_assets: tuple[str, ...] = field(default_factory=tuple)
    allowed_pay_to: tuple[str, ...] = field(default_factory=tuple)
    rate_limits: RateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY

    def allows(
        self,
        *,
        scheme: str | None,
        network: str | None,
        provider: str,
        asset: str | None = None,
        pay_to: str | None = None,
    ) -> bool:
        if self.status != "active":
            return False
        if provider not in self.enabled_providers:
            return False
        if scheme and scheme not in self.enabled_schemes:
            return False
        if network and network not in self.enabled_networks:
            return False
        if asset and self.allowed_assets and asset.lower() not in self.allowed_assets:
            return False
        return not (pay_to and self.allowed_pay_to and pay_to.lower() not in self.allowed_pay_to)


@dataclass(frozen=True)
class SellerAccountConfig:
    seller_account_id: str
    tenant_id: str = "production"
    name: str = "Default seller"
    environment: str = "testnet"
    status: str = "active"
    api_keys: tuple[str, ...] = field(default_factory=tuple)
    payment_profiles: tuple[PaymentProfileConfig, ...] = field(default_factory=tuple)
    enabled_networks: tuple[str, ...] = ("eip155:5042002",)
    enabled_schemes: tuple[str, ...] = ("exact",)
    enabled_providers: tuple[str, ...] = ("exact_evm",)
    allowed_assets: tuple[str, ...] = field(default_factory=tuple)
    allowed_pay_to: tuple[str, ...] = field(default_factory=tuple)
    rate_limits: RateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY

    def default_payment_profile(self) -> PaymentProfileConfig:
        if self.payment_profiles:
            return self.payment_profiles[0]
        return PaymentProfileConfig(
            payment_profile_id="default",
            seller_account_id=self.seller_account_id,
            name=self.name,
            status=self.status,
            enabled_networks=self.enabled_networks,
            enabled_schemes=self.enabled_schemes,
            enabled_providers=self.enabled_providers,
            allowed_assets=self.allowed_assets,
            allowed_pay_to=self.allowed_pay_to,
            rate_limits=self.rate_limits,
        )

    def payment_profile(self, payment_profile_id: str) -> PaymentProfileConfig:
        for profile in self.payment_profiles:
            if profile.payment_profile_id == payment_profile_id:
                return profile
        if payment_profile_id == "default":
            return self.default_payment_profile()
        raise KeyError(payment_profile_id)

    def allows(
        self,
        *,
        scheme: str | None,
        network: str | None,
        provider: str,
        asset: str | None = None,
        pay_to: str | None = None,
    ) -> bool:
        return self.default_payment_profile().allows(
            scheme=scheme,
            network=network,
            provider=provider,
            asset=asset,
            pay_to=pay_to,
        )


@dataclass(frozen=True)
class ResolvedSellerAccess:
    seller_account: SellerAccountConfig
    payment_profile: PaymentProfileConfig
    api_key_prefix: str | None = None


class StaticSellerAccountResolver:
    """In-memory resolver for tests and developer-only harnesses."""

    hosted_safe = False

    def __init__(self, seller_accounts: list[SellerAccountConfig] | None = None):
        default = SellerAccountConfig(seller_account_id="default")
        self._seller_accounts = {
            seller_account.seller_account_id: seller_account
            for seller_account in (seller_accounts or [default])
        }
        self._api_key_index: dict[str, ResolvedSellerAccess] = {}
        for seller_account in self._seller_accounts.values():
            default_profile = seller_account.default_payment_profile()
            for key in seller_account.api_keys:
                self._register_api_key(
                    key,
                    ResolvedSellerAccess(
                        seller_account=seller_account,
                        payment_profile=default_profile,
                        api_key_prefix=seller_key_prefix(key),
                    ),
                )
            for profile in seller_account.payment_profiles:
                profile_keys = getattr(profile, "api_keys", ())
                for key in profile_keys:
                    self._register_api_key(
                        key,
                        ResolvedSellerAccess(
                            seller_account=seller_account,
                            payment_profile=profile,
                            api_key_prefix=seller_key_prefix(key),
                        ),
                    )

    def resolve(
        self,
        *,
        authorization: str | None = None,
        require_auth: bool = False,
    ) -> SellerAccountConfig:
        return self.resolve_access(
            authorization=authorization, require_auth=require_auth
        ).seller_account

    def resolve_access(
        self,
        *,
        authorization: str | None = None,
        require_auth: bool = False,
    ) -> ResolvedSellerAccess:
        token = _bearer_token(authorization)
        if token:
            access = self._api_key_index.get(token)
            if access is None:
                raise PermissionError("Invalid facilitator API key")
            if isinstance(access, SellerAccountConfig):
                access = ResolvedSellerAccess(
                    seller_account=access,
                    payment_profile=access.default_payment_profile(),
                    api_key_prefix=seller_key_prefix(token),
                )
            if access.seller_account.status != "active":
                raise PermissionError("Seller account is not active")
            if access.payment_profile.status != "active":
                raise PermissionError("Payment profile is not active")
            return access

        if require_auth:
            raise PermissionError("Seller API-key authentication is required")

        seller_account = self._seller_accounts["default"]
        return ResolvedSellerAccess(
            seller_account=seller_account,
            payment_profile=seller_account.default_payment_profile(),
            api_key_prefix=None,
        )

    def health_check(self) -> bool:
        return True

    def _register_api_key(self, api_key: str, access: ResolvedSellerAccess) -> None:
        if api_key in self._api_key_index:
            raise ValueError("Duplicate seller API key configured")
        self._api_key_index[api_key] = access


@dataclass(frozen=True)
class IssuedSellerKey:
    key_id: str
    seller_account_id: str
    payment_profile_id: str
    key: str
    key_hash: str
    key_prefix: str


def issue_seller_api_key(
    seller_account_id: str, *, payment_profile_id: str = "default"
) -> IssuedSellerKey:
    return _issue_seller_key(
        seller_account_id=seller_account_id,
        payment_profile_id=payment_profile_id,
        prefix=API_KEY_PREFIX,
    )


def seller_key_hash(key: str, *, pepper: str | None = None) -> str:
    value = str(key).strip()
    if not value:
        raise ValueError("seller API key is required")
    secret = pepper if pepper is not None else os.getenv("OMNICLAW_HOSTED_API_KEY_HASH_PEPPER")
    if secret:
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(value.encode()).hexdigest()


def seller_key_prefix(key: str) -> str:
    value = str(key).strip()
    if not value:
        return ""
    return value[:16]


def seller_key_matches(key: str, expected_hash: str) -> bool:
    return compare_digest(seller_key_hash(key), expected_hash)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _issue_seller_key(
    *, seller_account_id: str, payment_profile_id: str, prefix: str
) -> IssuedSellerKey:
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return IssuedSellerKey(
        key_id=f"key_{secrets.token_urlsafe(18)}",
        seller_account_id=seller_account_id,
        payment_profile_id=payment_profile_id,
        key=token,
        key_hash=seller_key_hash(token),
        key_prefix=seller_key_prefix(token),
    )
