from __future__ import annotations

import pytest

from hosted_facilitator.hosted.limits import (
    HostedRateLimitEndpoint,
    InMemoryFixedWindowRateLimiter,
    RateLimitPolicy,
    RateLimitRequest,
    RedisFixedWindowRateLimiter,
)


def _request(endpoint: HostedRateLimitEndpoint = HostedRateLimitEndpoint.VERIFY):
    return RateLimitRequest(
        endpoint=endpoint,
        seller_account_id="seller-a",
        tenant_id="tenant-a",
        client_host="203.0.113.10",
        policy=RateLimitPolicy(
            seller_requests_per_minute=10,
            verify_per_minute=2,
            settle_per_minute=1,
            invalid_requests_per_minute=1,
            ip_requests_per_minute=10,
        ),
    )


@pytest.mark.asyncio
async def test_in_memory_fixed_window_limiter_allows_until_limit_then_rejects():
    limiter = InMemoryFixedWindowRateLimiter(clock=lambda: 0)

    first = await limiter.check(_request())
    second = await limiter.check(_request())
    third = await limiter.check(_request())

    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0
    assert third.allowed is False
    assert third.retry_after_seconds == 60


@pytest.mark.asyncio
async def test_in_memory_fixed_window_limiter_resets_on_new_window():
    now = {"value": 0.0}
    limiter = InMemoryFixedWindowRateLimiter(clock=lambda: now["value"])

    await limiter.check(_request(endpoint=HostedRateLimitEndpoint.SETTLE))
    rejected = await limiter.check(_request(endpoint=HostedRateLimitEndpoint.SETTLE))
    now["value"] = 61.0
    allowed = await limiter.check(_request(endpoint=HostedRateLimitEndpoint.SETTLE))

    assert rejected.allowed is False
    assert allowed.allowed is True


def test_redis_fixed_window_limiter_requires_explicit_url(monkeypatch):
    monkeypatch.delenv("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL", raising=False)

    with pytest.raises(RuntimeError, match="OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL"):
        RedisFixedWindowRateLimiter()


@pytest.mark.asyncio
async def test_redis_fixed_window_limiter_builds_atomic_multi_bucket_check():
    class _FakeRedis:
        def __init__(self):
            self.calls = []
            self.closed = False
            self.pings = 0

        async def ping(self):
            self.pings += 1
            return True

        async def eval(self, script, num_keys, *args):
            self.calls.append((script, num_keys, args))
            return [1, 0, 2, 1]

        async def aclose(self):
            self.closed = True

    fake = _FakeRedis()
    limiter = RedisFixedWindowRateLimiter("redis://example.invalid/0", prefix="test")
    limiter._client = fake

    await limiter.initialize()
    decision = await limiter.check(_request())
    await limiter.close()

    assert fake.pings == 1
    assert decision.allowed is True
    assert decision.limit == 2
    assert decision.remaining == 1
    assert fake.calls
    _script, num_keys, args = fake.calls[0]
    keys = args[:num_keys]
    assert num_keys == 3
    assert any("ip:203.0.113.10:all" in key for key in keys)
    assert any("seller_account:seller-a:verify" in key for key in keys)
    assert any("seller_account:seller-a:all" in key for key in keys)
    assert all("{seller_account.seller-a}" in key for key in keys)
    assert fake.closed is True
