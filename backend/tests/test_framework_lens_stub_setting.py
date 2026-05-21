"""PR-C2c: ``framework_lens.is_stub_enabled()`` reads from the cached
``Settings.framework_lens_stub`` flag (env name preserved as
``AUTOESSAY_FRAMEWORK_LENS_STUB``). Tests that flip the env at runtime
must call ``get_settings.cache_clear()`` first; this test file pins
that contract so a future refactor doesn't silently regress it.
"""

from __future__ import annotations

import pytest

from autoessay.config import get_settings
from autoessay.framework_lens import is_stub_enabled


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Cache reset around each test so env mutations land in Settings."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_is_stub_enabled_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOESSAY_FRAMEWORK_LENS_STUB", raising=False)
    get_settings.cache_clear()
    assert is_stub_enabled() is False
    assert get_settings().framework_lens_stub is False


def test_is_stub_enabled_true_when_env_set_and_cache_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FRAMEWORK_LENS_STUB", "1")
    get_settings.cache_clear()
    assert is_stub_enabled() is True
    assert get_settings().framework_lens_stub is True


def test_is_stub_enabled_ignores_env_change_without_cache_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents the ``cache_clear()`` requirement: without it, the
    cached Settings continues to report the value at first instantiation.
    A future refactor that drops this requirement should also update
    this test (it is the contract pin)."""
    monkeypatch.delenv("AUTOESSAY_FRAMEWORK_LENS_STUB", raising=False)
    get_settings.cache_clear()
    assert is_stub_enabled() is False
    monkeypatch.setenv("AUTOESSAY_FRAMEWORK_LENS_STUB", "1")
    # Cache NOT cleared — Settings still reports the prior value.
    assert is_stub_enabled() is False
    # After cache_clear, the new env value lands.
    get_settings.cache_clear()
    assert is_stub_enabled() is True
