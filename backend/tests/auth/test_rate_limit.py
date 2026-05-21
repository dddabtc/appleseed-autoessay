"""PR-B: Redis rate-limit primitive tests (no real Redis needed)."""

from __future__ import annotations

from typing import Any

from autoessay.auth import rate_limit


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[bytes, int] = {}
        self.ttls: dict[bytes, int] = {}

    @staticmethod
    def _key(key: str | bytes) -> bytes:
        return key.encode("utf-8") if isinstance(key, str) else key

    def get(self, key: str) -> bytes | None:
        value = self.store.get(self._key(key))
        return str(value).encode("ascii") if value is not None else None

    def ttl(self, key: str) -> int:
        return self.ttls.get(self._key(key), -2)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def delete(self, key: str) -> None:
        bkey = self._key(key)
        self.store.pop(bkey, None)
        self.ttls.pop(bkey, None)


class FakePipeline:
    def __init__(self, parent: FakeRedis) -> None:
        self.parent = parent
        self.ops: list[tuple[str, str, int]] = []

    def incr(self, key: str) -> FakePipeline:
        self.ops.append(("incr", key, 0))
        return self

    def expire(self, key: str, seconds: int) -> FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    def execute(self) -> list[Any]:
        results: list[Any] = []
        for op_name, key, payload in self.ops:
            bkey = self.parent._key(key)
            if op_name == "incr":
                self.parent.store[bkey] = self.parent.store.get(bkey, 0) + 1
                results.append(self.parent.store[bkey])
            elif op_name == "expire":
                self.parent.ttls[bkey] = payload
                results.append(True)
        return results


def test_check_returns_unblocked_for_fresh_ip() -> None:
    redis = FakeRedis()
    decision = rate_limit.check(redis, "1.2.3.4")
    assert not decision.blocked
    assert decision.fails_in_window == 0


def test_record_failure_increments_and_sets_ttl() -> None:
    redis = FakeRedis()
    for expected_count in range(1, 6):
        new_count = rate_limit.record_failure(redis, "1.2.3.4")
        assert new_count == expected_count
    assert redis.ttls[redis._key("autoessay:auth:login_fails:1.2.3.4")] == 3600


def test_check_blocks_at_threshold() -> None:
    redis = FakeRedis()
    for _ in range(rate_limit.LOGIN_FAIL_THRESHOLD):
        rate_limit.record_failure(redis, "1.2.3.4")
    decision = rate_limit.check(redis, "1.2.3.4")
    assert decision.blocked
    assert decision.retry_after_seconds > 0
    assert decision.fails_in_window == rate_limit.LOGIN_FAIL_THRESHOLD


def test_clear_resets_counter() -> None:
    redis = FakeRedis()
    for _ in range(rate_limit.LOGIN_FAIL_THRESHOLD):
        rate_limit.record_failure(redis, "1.2.3.4")
    rate_limit.clear(redis, "1.2.3.4")
    decision = rate_limit.check(redis, "1.2.3.4")
    assert not decision.blocked
    assert decision.fails_in_window == 0


def test_client_ip_prefers_cf_connecting_ip() -> None:
    class _Req:
        headers = {
            "cf-connecting-ip": "203.0.113.99",
            "x-forwarded-for": "10.0.0.1, 198.51.100.1",
        }

        class client:
            host = "127.0.0.1"

    assert rate_limit.client_ip(_Req()) == "203.0.113.99"


def test_client_ip_falls_back_to_xff_first_hop() -> None:
    class _Req:
        headers = {"x-forwarded-for": "203.0.113.7, 198.51.100.1"}

        class client:
            host = "127.0.0.1"

    assert rate_limit.client_ip(_Req()) == "203.0.113.7"


def test_client_ip_final_fallback_is_request_client_host() -> None:
    class _Req:
        headers: dict[str, str] = {}

        class client:
            host = "127.0.0.1"

    assert rate_limit.client_ip(_Req()) == "127.0.0.1"
