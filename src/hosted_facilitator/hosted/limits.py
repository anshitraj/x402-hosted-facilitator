from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Any, Protocol


class HostedRateLimitEndpoint(StrEnum):
    SUPPORTED = "supported"
    VERIFY = "verify"
    SETTLE = "settle"
    INVALID = "invalid"


@dataclass(frozen=True)
class RateLimitPolicy:
    seller_requests_per_minute: int | None = 600
    supported_per_minute: int | None = 600
    verify_per_minute: int | None = 300
    settle_per_minute: int | None = 60
    invalid_requests_per_minute: int | None = 30
    ip_requests_per_minute: int | None = 1200

    def limit_for(self, endpoint: HostedRateLimitEndpoint) -> int | None:
        if endpoint == HostedRateLimitEndpoint.SUPPORTED:
            return self.supported_per_minute
        if endpoint == HostedRateLimitEndpoint.VERIFY:
            return self.verify_per_minute
        if endpoint == HostedRateLimitEndpoint.SETTLE:
            return self.settle_per_minute
        if endpoint == HostedRateLimitEndpoint.INVALID:
            return self.invalid_requests_per_minute
        raise ValueError(f"unsupported hosted rate-limit endpoint: {endpoint}")


DEFAULT_RATE_LIMIT_POLICY = RateLimitPolicy()


@dataclass(frozen=True)
class RateLimitBucket:
    key: str
    limit: int
    window_seconds: int = 60


@dataclass(frozen=True)
class RateLimitRequest:
    endpoint: HostedRateLimitEndpoint
    seller_account_id: str | None
    tenant_id: str | None
    client_host: str | None
    policy: RateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0
    limit: int | None = None
    remaining: int | None = None
    reset_after_seconds: int | None = None
    reason: str | None = None

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.retry_after_seconds > 0:
            headers["Retry-After"] = str(self.retry_after_seconds)
        if self.limit is not None:
            headers["X-RateLimit-Limit"] = str(self.limit)
        if self.remaining is not None:
            headers["X-RateLimit-Remaining"] = str(max(self.remaining, 0))
        if self.reset_after_seconds is not None:
            headers["X-RateLimit-Reset"] = str(self.reset_after_seconds)
        return headers


class HostedRateLimiter(Protocol):
    hosted_safe: bool

    async def check(self, request: RateLimitRequest) -> RateLimitDecision: ...


class HostedRateLimitExceededError(Exception):
    def __init__(self, decision: RateLimitDecision):
        super().__init__("hosted facilitator rate limit exceeded")
        self.decision = decision


class InMemoryFixedWindowRateLimiter:
    hosted_safe = False

    def __init__(self, *, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = RLock()
        self._counters: dict[tuple[str, int], int] = {}

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        buckets = _buckets_for_request(request)
        if not buckets:
            return RateLimitDecision(allowed=True)
        now = self._clock()
        with self._lock:
            decisions = [
                _decision_for_bucket(bucket, self._count(bucket, now)) for bucket in buckets
            ]
            rejected = [decision for decision in decisions if not decision.allowed]
            if rejected:
                return _most_restrictive(rejected)
            for bucket in buckets:
                self._increment(bucket, now)
            return _most_restrictive(
                [
                    _decision_for_bucket(bucket, self._count(bucket, now), consumed=True)
                    for bucket in buckets
                ]
            )

    def health_check(self) -> bool:
        return True

    def _window_id(self, bucket: RateLimitBucket, now: float) -> int:
        return math.floor(now / bucket.window_seconds)

    def _count(self, bucket: RateLimitBucket, now: float) -> int:
        return self._counters.get((bucket.key, self._window_id(bucket, now)), 0)

    def _increment(self, bucket: RateLimitBucket, now: float) -> None:
        window_id = self._window_id(bucket, now)
        self._counters[(bucket.key, window_id)] = self._counters.get((bucket.key, window_id), 0) + 1


class RedisFixedWindowRateLimiter:
    hosted_safe = True

    _SCRIPT = """
local rejected = 0
local retry_after = 0
local limit = nil
local remaining = nil
for i = 1, #KEYS do
  local bucket_limit = tonumber(ARGV[(i - 1) * 2 + 1])
  local ttl = tonumber(ARGV[(i - 1) * 2 + 2])
  local current = tonumber(redis.call("GET", KEYS[i]) or "0")
  if current >= bucket_limit then
    rejected = 1
    local key_ttl = redis.call("TTL", KEYS[i])
    if key_ttl < 0 then key_ttl = ttl end
    if key_ttl > retry_after then retry_after = key_ttl end
    if limit == nil or bucket_limit < limit then
      limit = bucket_limit
      remaining = bucket_limit - current
    end
  end
end
if rejected == 1 then
  return {0, retry_after, limit or 0, remaining or 0}
end
local min_limit = nil
local min_remaining = nil
for i = 1, #KEYS do
  local bucket_limit = tonumber(ARGV[(i - 1) * 2 + 1])
  local ttl = tonumber(ARGV[(i - 1) * 2 + 2])
  local current = tonumber(redis.call("INCR", KEYS[i]))
  if current == 1 then redis.call("EXPIRE", KEYS[i], ttl) end
  local bucket_remaining = bucket_limit - current
  if min_remaining == nil or bucket_remaining < min_remaining then
    min_limit = bucket_limit
    min_remaining = bucket_remaining
  end
end
return {1, 0, min_limit or 0, min_remaining or 0}
"""

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        prefix: str = "omniclaw:hosted:rl",
        socket_connect_timeout: float = 1.0,
        socket_timeout: float = 1.0,
        health_check_interval: int = 30,
    ):
        self._redis_url = redis_url or os.environ.get("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL")
        if not self._redis_url:
            raise RuntimeError("OMNICLAW_HOSTED_RATE_LIMIT_REDIS_URL is required")
        self._prefix = prefix.rstrip(":")
        self._socket_connect_timeout = socket_connect_timeout
        self._socket_timeout = socket_timeout
        self._health_check_interval = health_check_interval
        self._client: Any | None = None

    async def initialize(self) -> None:
        client = await self._get_client()
        await client.ping()

    async def health_check(self) -> bool:
        client = await self._get_client()
        return bool(await client.ping())

    async def check(self, request: RateLimitRequest) -> RateLimitDecision:
        buckets = _buckets_for_request(request)
        if not buckets:
            return RateLimitDecision(allowed=True)
        client = await self._get_client()
        now = int(time.time())
        keys = [self._redis_key(bucket, now, request) for bucket in buckets]
        args: list[int] = []
        for bucket in buckets:
            args.extend([bucket.limit, bucket.window_seconds])
        result = await client.eval(self._SCRIPT, len(keys), *keys, *args)
        allowed = int(result[0]) == 1
        retry_after = int(result[1])
        limit = int(result[2]) if int(result[2]) > 0 else None
        remaining = int(result[3]) if limit is not None else None
        return RateLimitDecision(
            allowed=allowed,
            retry_after_seconds=retry_after,
            limit=limit,
            remaining=remaining,
            reset_after_seconds=retry_after if retry_after > 0 else 60,
            reason=None if allowed else "rate_limited",
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(self):
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=self._socket_connect_timeout,
                socket_timeout=self._socket_timeout,
                health_check_interval=self._health_check_interval,
                retry_on_timeout=False,
            )
        return self._client

    def _redis_key(self, bucket: RateLimitBucket, now: int, request: RateLimitRequest) -> str:
        window_id = now // bucket.window_seconds
        return f"{self._prefix}:{{{_request_hash_tag(request)}}}:{bucket.key}:{window_id}"


def _buckets_for_request(request: RateLimitRequest) -> list[RateLimitBucket]:
    buckets: list[RateLimitBucket] = []
    policy = request.policy
    if request.client_host and policy.ip_requests_per_minute is not None:
        buckets.append(
            RateLimitBucket(
                key=f"ip:{_safe_key_part(request.client_host)}:all",
                limit=policy.ip_requests_per_minute,
            )
        )
    endpoint_limit = policy.limit_for(request.endpoint)
    if request.seller_account_id and endpoint_limit is not None:
        buckets.append(
            RateLimitBucket(
                key=f"seller_account:{_safe_key_part(request.seller_account_id)}:{request.endpoint.value}",
                limit=endpoint_limit,
            )
        )
    if (
        request.seller_account_id
        and request.endpoint != HostedRateLimitEndpoint.INVALID
        and policy.seller_requests_per_minute is not None
    ):
        buckets.append(
            RateLimitBucket(
                key=f"seller_account:{_safe_key_part(request.seller_account_id)}:all",
                limit=policy.seller_requests_per_minute,
            )
        )
    if request.client_host and request.endpoint == HostedRateLimitEndpoint.INVALID:
        invalid_limit = policy.invalid_requests_per_minute
        if invalid_limit is not None:
            buckets.append(
                RateLimitBucket(
                    key=f"ip:{_safe_key_part(request.client_host)}:invalid",
                    limit=invalid_limit,
                )
            )
    return buckets


def _decision_for_bucket(
    bucket: RateLimitBucket,
    count: int,
    *,
    consumed: bool = False,
) -> RateLimitDecision:
    allowed = count <= bucket.limit if consumed else count < bucket.limit
    remaining = bucket.limit - count if consumed else bucket.limit - count - 1
    return RateLimitDecision(
        allowed=allowed,
        retry_after_seconds=0 if allowed else bucket.window_seconds,
        limit=bucket.limit,
        remaining=max(remaining, 0),
        reset_after_seconds=bucket.window_seconds,
        reason=None if allowed else "rate_limited",
    )


def _most_restrictive(decisions: list[RateLimitDecision]) -> RateLimitDecision:
    if not decisions:
        return RateLimitDecision(allowed=True)
    rejected = [decision for decision in decisions if not decision.allowed]
    if rejected:
        return max(rejected, key=lambda decision: decision.retry_after_seconds)
    return min(
        decisions,
        key=lambda decision: (
            decision.remaining if decision.remaining is not None else math.inf,
            decision.limit if decision.limit is not None else math.inf,
        ),
    )


def _safe_key_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in value)[:128]


def _request_hash_tag(request: RateLimitRequest) -> str:
    if request.seller_account_id:
        return f"seller_account.{_safe_key_part(request.seller_account_id)}"
    if request.client_host:
        return f"ip.{_safe_key_part(request.client_host)}"
    return "anonymous"
