"""Redis-backed login rate limiter (PR-B).

Single rule: 10 failed `POST /api/auth/login` attempts from one IP
within a sliding 1-hour window locks that IP out for the remainder
of the hour. Successful login clears the counter immediately.

We deliberately do not lock per-username — that would let an
attacker DoS a known account by failing on purpose. Per-IP keeps
real users from being locked out by someone else's bad guesses
(modulo NAT, which is unavoidable and acceptable at our internal-
testing scale).

IP source priority, in order:

  1. ``CF-Connecting-IP`` — Cloudflare's authoritative client IP.
     Required since prod sits behind Cloudflare; without it, every
     request looks like a Cloudflare edge IP.
  2. First hop of ``X-Forwarded-For`` — for HAProxy / nginx setups
     where Cloudflare isn't terminating directly.
  3. ``request.client.host`` — final fallback for local dev where
     no proxy is in front.

Production deploy must ensure HAProxy / nginx forwards one of those
headers untouched; otherwise rate limiting is effectively global
(everyone shares one IP), which is still fail-safe but blunt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from redis import Redis

LOGIN_FAIL_WINDOW_SECONDS = 60 * 60  # 1 hour
LOGIN_FAIL_THRESHOLD = 10


@dataclass(frozen=True)
class RateLimitDecision:
    blocked: bool
    retry_after_seconds: int  # 0 when not blocked
    fails_in_window: int


def client_ip(request: Request) -> str:
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _key(ip: str) -> str:
    return f"autoessay:auth:login_fails:{ip}"


def check(redis: Redis, ip: str) -> RateLimitDecision:
    # ``redis-py``'s sync / async clients share one type, so mypy
    # widens ``get`` / ``ttl`` returns to ``Awaitable[Any] | Any``.
    # We construct the client via ``Redis.from_url`` (sync) so the
    # narrowing below is safe; the ``isinstance`` checks are also
    # the runtime guard for our ``_FakeRedis`` test double.
    raw = redis.get(_key(ip))
    fails = int(raw) if isinstance(raw, (str, bytes, int)) else 0
    if fails >= LOGIN_FAIL_THRESHOLD:
        ttl_value = redis.ttl(_key(ip))
        retry_after = (
            ttl_value if isinstance(ttl_value, int) and ttl_value > 0 else LOGIN_FAIL_WINDOW_SECONDS
        )
        return RateLimitDecision(
            blocked=True,
            retry_after_seconds=retry_after,
            fails_in_window=fails,
        )
    return RateLimitDecision(blocked=False, retry_after_seconds=0, fails_in_window=fails)


def record_failure(redis: Redis, ip: str) -> int:
    """Atomically INCR + reset the 1-hour TTL on every failure.

    Resetting TTL on each failure keeps the lockout window rolling
    so an attacker can't sneak under the threshold by spreading
    attempts across the hour boundary. Returns the new fail count.
    """

    pipe = redis.pipeline()
    pipe.incr(_key(ip))
    pipe.expire(_key(ip), LOGIN_FAIL_WINDOW_SECONDS)
    new_count, _ = pipe.execute()
    return int(new_count)


def clear(redis: Redis, ip: str) -> None:
    redis.delete(_key(ip))
