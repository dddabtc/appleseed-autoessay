import json

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from autoessay.config import get_settings
from autoessay.main import app


async def test_kernel_suggest_returns_llm_suggestion(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import autoessay.kernel_suggest as kernel_suggest

    calls: dict[str, object] = {}

    async def fake_chat_completion(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["model"] = kwargs["model"]
        calls["max_tokens"] = kwargs["max_tokens"]
        calls["response_format"] = kwargs["response_format"]
        calls["validate_json_content"] = kwargs["validate_json_content"]
        return {
            "content": json.dumps(
                {
                    "observed_puzzle": (
                        "既有研究将货币承诺解释为单一政策选择，但材料中显示国内政治与国际约束之间存在持续张力。"
                    ),
                    "tentative_question": "战后货币承诺如何同时受国内政治与国际制度约束？",
                    "scope": "1944-1971 年布雷顿森林体系下的美国与西欧货币政策讨论。",
                    "method_preference": "制度史分析与政策文本细读",
                    "theory_preference": "历史制度主义",
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(kernel_suggest, "chat_completion", fake_chat_completion)
    get_settings.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs/kernel_suggest",
            json={
                "title": "布雷顿森林体系下的货币承诺",
                "domain_id": "financial_history",
                "language": "zh",
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["suggestion"]["tentative_question"] == "战后货币承诺如何同时受国内政治与国际制度约束？"
    )
    assert len(body["suggestion"]["observed_puzzle"]) >= 30
    assert len(body["suggestion"]["scope"]) <= 200
    assert body["model"] == "gpt-5.4"
    assert body["max_tokens"] == 900
    assert calls == {
        "model": "gpt-5.4",
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
        "validate_json_content": True,
    }


async def test_kernel_suggest_rejects_missing_or_short_title(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs/kernel_suggest",
            json={"title": "abc", "domain_id": "financial_history", "language": "zh"},
        )

    assert response.status_code == 422
    assert "title" in response.text


async def test_kernel_suggest_reports_llm_failure(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import autoessay.kernel_suggest as kernel_suggest

    async def failing_chat_completion(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(kernel_suggest, "chat_completion", failing_chat_completion)
    get_settings.cache_clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs/kernel_suggest",
            json={
                "title": "布雷顿森林体系下的货币承诺",
                "domain_id": "financial_history",
                "language": "zh",
            },
        )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "kernel_suggest_failed"


async def test_kernel_suggest_runs_safety_gate_on_generated_fields(
    app_session,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import autoessay.main as main_mod

    monkeypatch.setenv("AUTOESSAY_KERNEL_SUGGEST_STUB", "1")
    get_settings.cache_clear()

    def block_generated_kernel(_kernel):  # type: ignore[no-untyped-def]
        raise HTTPException(
            status_code=400,
            detail={"code": "safety_gate_blocked", "field_path": "kernel.observed_puzzle"},
        )

    monkeypatch.setattr(main_mod, "_enforce_research_kernel_safety", block_generated_kernel)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs/kernel_suggest",
            json={
                "title": "布雷顿森林体系下的货币承诺",
                "domain_id": "financial_history",
                "language": "zh",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "safety_gate_blocked"
