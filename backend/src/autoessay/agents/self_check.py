"""Self-check report — paper-quality-spec.md §七 (13 checklist items).

Runs an LLM-driven review of the final manuscript against the 13-item
self-check from the user's writing spec. Produces a structured
``SelfCheckReport`` and a markdown document. The report flags each
item as ``pass`` / ``warn`` / ``fail`` with a one-sentence justification
and an optional fix suggestion.

The self-check is best-effort: any LLM error / parse error returns a
default report marked ``incomplete`` so the exporter never crashes,
but the user sees the missing review in the manifest.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError, validator

from autoessay.agents._language import language_directive
from autoessay.config import get_settings
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    hash_text,
    run_llm_step,
)
from autoessay.harness.runner import SchemaViolationError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from autoessay.models import Run

# 13 checklist items, keyed by stable id. Wording in zh because the
# canonical specification is Chinese and that is the language the
# evaluator LLM should reason in. The English/Japanese label maps
# below are for rendering the markdown output in non-zh manuscripts.
SELF_CHECK_ITEMS: tuple[tuple[str, str], ...] = (
    ("title_accurate", "题目是否准确反映论文内容？"),
    ("abstract_complete", "摘要是否包含问题、方法、发现和贡献？"),
    ("introduction_clear", "引言是否提出了明确研究问题？"),
    ("literature_classified", "文献综述是否完成分类评价（不是简单罗列）？"),
    ("framework_clear", "理论框架是否清楚？"),
    ("central_argument", "正文是否围绕同一个中心论点展开？"),
    ("section_progression", "每一节之间是否有递进关系？"),
    ("conclusion_answers", "结论是否真正回答了研究问题？"),
    ("citation_correspondence", "正文引用和参考文献是否一一对应？"),
    ("verifiable_sources", "是否存在无法核验的文献？（理想答案：不存在）"),
    ("no_fabrication", "是否存在编造数据/案例/访谈？（理想答案：不存在）"),
    ("no_slogans", "是否有明显口号化、散文化或新闻评论化表达？（理想答案：没有）"),
    ("format_compliant", "是否符合目标期刊或中文社科期刊基本格式？"),
)

_ITEM_LABELS_EN: dict[str, str] = {
    "title_accurate": "Title accurately reflects the paper",
    "abstract_complete": "Abstract covers problem / method / finding / contribution",
    "introduction_clear": "Introduction states a clear research question",
    "literature_classified": "Literature review classifies and evaluates rather than rolls call",
    "framework_clear": "Theoretical framework is articulated",
    "central_argument": "Body sections track one central argument",
    "section_progression": "Sections progress, not parallel listing",
    "conclusion_answers": "Conclusion actually answers the research question",
    "citation_correspondence": "In-text citations match the reference list",
    "verifiable_sources": "No unverifiable references",
    "no_fabrication": "No fabricated data / case / interview",
    "no_slogans": "No slogan / journalistic / essayistic phrasing",
    "format_compliant": "Format matches target-journal expectations",
}

_ITEM_LABELS_JA: dict[str, str] = {
    "title_accurate": "題目は内容を正確に反映",
    "abstract_complete": "要旨に問題・方法・発見・貢献",
    "introduction_clear": "序論で研究課題を明示",
    "literature_classified": "先行研究を分類評価",
    "framework_clear": "理論枠組が明確",
    "central_argument": "本論が中心論点を一貫",
    "section_progression": "各節に進展がある",
    "conclusion_answers": "結論が研究課題に応答",
    "citation_correspondence": "本文引用と参考文献が一致",
    "verifiable_sources": "検証不能な文献なし",
    "no_fabrication": "捏造データ・事例・取材なし",
    "no_slogans": "スローガン的・随筆的・コメント的表現なし",
    "format_compliant": "投稿形式に適合",
}

Verdict = Literal["pass", "warn", "fail", "incomplete"]


@dataclass(frozen=True)
class SelfCheckItem:
    item_id: str
    question: str
    verdict: Verdict
    rationale: str
    fix: str = ""


@dataclass(frozen=True)
class SelfCheckReport:
    items: tuple[SelfCheckItem, ...]

    @property
    def overall_verdict(self) -> Verdict:
        if any(item.verdict == "fail" for item in self.items):
            return "fail"
        if any(item.verdict == "warn" for item in self.items):
            return "warn"
        if any(item.verdict == "incomplete" for item in self.items):
            return "incomplete"
        return "pass"

    @property
    def pass_count(self) -> int:
        return sum(1 for i in self.items if i.verdict == "pass")

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.items if i.verdict == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for i in self.items if i.verdict == "warn")


class _LLMItemOutput(BaseModel):
    item_id: str
    verdict: str
    rationale: str = ""
    fix: str = ""

    @validator("verdict")
    def _check_verdict(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in {"pass", "warn", "fail", "incomplete"}:
            return "incomplete"
        return cleaned

    class Config:
        extra = "ignore"


class _LLMReportOutput(BaseModel):
    items: list[_LLMItemOutput]

    class Config:
        extra = "ignore"


def run_self_check(
    *,
    run: Run,
    session: Session,
    manuscript_markdown: str,
    project_language: str,
) -> SelfCheckReport:
    """Run the 13-item self-check against ``manuscript_markdown``.

    Stub mode (``AUTOESSAY_SELF_CHECK_STUB=1``) returns every item as
    ``incomplete`` with rationale "stub mode" — used in tests so the
    exporter does not need a live LLM to write the report.

    PR-D2.1 (2026-05-03): the LLM call now goes through
    ``harness.run_llm_step``. Caller (exporter) must thread ``run`` and
    ``session`` so the standalone ``AuditWriter`` can be created.
    """
    settings = get_settings()
    if getattr(settings, "self_check_stub", False):
        return _stub_report()
    audit = AuditWriter(
        session=session,
        run_dir=Path(run.run_dir),
        agent_name="SelfCheck",
    )
    try:
        result = asyncio.run(
            _run_self_check_via_llm(
                run=run,
                audit=audit,
                manuscript_markdown=manuscript_markdown,
                project_language=project_language,
            ),
        )
    except Exception:  # noqa: BLE001 - exporter must not crash on LLM hiccup
        return _incomplete_report("self-check LLM call raised an exception")
    if result is None:
        return _incomplete_report("self-check LLM returned an unparseable response")
    return result


def _stub_report() -> SelfCheckReport:
    items = tuple(
        SelfCheckItem(
            item_id=item_id,
            question=question,
            verdict="incomplete",
            rationale="stub mode",
            fix="",
        )
        for item_id, question in SELF_CHECK_ITEMS
    )
    return SelfCheckReport(items=items)


def _incomplete_report(reason: str) -> SelfCheckReport:
    items = tuple(
        SelfCheckItem(
            item_id=item_id,
            question=question,
            verdict="incomplete",
            rationale=reason,
            fix="",
        )
        for item_id, question in SELF_CHECK_ITEMS
    )
    return SelfCheckReport(items=items)


async def _run_self_check_via_llm(
    *,
    run: Run,
    audit: AuditWriter,
    manuscript_markdown: str,
    project_language: str,
) -> SelfCheckReport | None:
    settings = get_settings()
    model = getattr(settings, "one_api_model", None) or "gpt-5.4-mini"
    item_prompts = [
        {"item_id": item_id, "question": question} for item_id, question in SELF_CHECK_ITEMS
    ]
    schema = {
        "items": [
            {
                "item_id": "see input",
                "verdict": "pass | warn | fail | incomplete",
                "rationale": "one-sentence justification grounded in the manuscript",
                "fix": "optional one-sentence concrete fix suggestion when not pass",
            },
        ],
    }
    system = (
        "You are a strict reviewer for a Chinese social-science journal. "
        "You evaluate the manuscript below against the supplied 13-item "
        "self-check list. For each item, decide pass / warn / fail / "
        "incomplete and justify briefly with a sentence that REFERS TO "
        "the manuscript content. Do not fabricate observations not "
        "present in the manuscript. Return strict JSON only. "
        + language_directive(project_language)
    )
    user = (
        "Self-check items (use the same item_id verbatim in your output):\n"
        f"{json.dumps(item_prompts, ensure_ascii=False)}\n\n"
        "Manuscript:\n"
        f"{manuscript_markdown[:24000]}\n\n"
        f"Return strict JSON matching this schema: "
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model,
        temperature=0.1,
        max_tokens=2200,
        response_format={"type": "json_object"},
        request_id="self_check",
        prompt_template_id="self_check.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="exports",
        step_id="self_check",
        user_id=None,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user,
        prompt_hash=hash_text(user),
        project_title="",
    )
    audit.start_invocation(context)
    try:
        response = await run_llm_step(
            request=request,
            hooks=HookRegistry(),
            context=context,
            output_schema=_LLMReportOutput,
            audit=audit,
        )
    except SchemaViolationError:
        return None
    parsed = response.parsed
    if not isinstance(parsed, _LLMReportOutput):
        try:
            parsed = _LLMReportOutput.parse_obj(parsed if isinstance(parsed, dict) else {})
        except ValidationError:
            return None
    questions = dict(SELF_CHECK_ITEMS)
    by_item: dict[str, _LLMItemOutput] = {}
    for entry in parsed.items:
        if entry.item_id in questions:
            by_item.setdefault(entry.item_id, entry)
    items: list[SelfCheckItem] = []
    for item_id, question in SELF_CHECK_ITEMS:
        match = by_item.get(item_id)
        if match is None:
            items.append(
                SelfCheckItem(
                    item_id=item_id,
                    question=question,
                    verdict="incomplete",
                    rationale="reviewer omitted this item",
                ),
            )
            continue
        items.append(
            SelfCheckItem(
                item_id=item_id,
                question=question,
                verdict=match.verdict,  # type: ignore[arg-type]
                rationale=match.rationale.strip(),
                fix=match.fix.strip(),
            ),
        )
    return SelfCheckReport(items=tuple(items))


_TITLE_BY_CODE: dict[str, str] = {
    "zh": "自检报告",
    "ja": "自己点検報告",
    "en": "Self-Check Report",
}
_OVERALL_LABEL_BY_CODE: dict[str, str] = {
    "zh": "总体判定",
    "ja": "総合判定",
    "en": "Overall verdict",
}
_FIX_LABEL_BY_CODE: dict[str, str] = {
    "zh": "建议修复",
    "ja": "推奨対応",
    "en": "Suggested fix",
}
_VERDICT_EMOJI: dict[str, str] = {
    "pass": "✅",
    "warn": "⚠️",
    "fail": "❌",
    "incomplete": "⏳",
}


def _counts_line(report: SelfCheckReport, code: str) -> str | None:
    incomplete = sum(1 for i in report.items if i.verdict == "incomplete")
    p, w, f = report.pass_count, report.warn_count, report.fail_count
    if code == "zh":
        return f"通过 {p} / 警告 {w} / 不通过 {f} / 未完成 {incomplete}"
    if code == "ja":
        return f"合格 {p} / 警告 {w} / 不合格 {f} / 未完了 {incomplete}"
    if code == "en":
        return f"pass {p} / warn {w} / fail {f} / incomplete {incomplete}"
    return None


def render_self_check_markdown(report: SelfCheckReport, project_language: str) -> str:
    code = (project_language or "en").lower()
    title = _TITLE_BY_CODE.get(code, "Self-Check Report")
    overall_label = _OVERALL_LABEL_BY_CODE.get(code, "Overall verdict")
    counts_label = _counts_line(report, code)
    lines: list[str] = [f"# {title}", "", f"**{overall_label}**: {report.overall_verdict}", ""]
    if counts_label:
        lines.append(counts_label)
        lines.append("")
    for item in report.items:
        label = _label_for_item(item.item_id, code, item.question)
        lines.append(f"## {label}")
        lines.append("")
        verdict_emoji = _VERDICT_EMOJI.get(item.verdict, "❓")
        lines.append(f"- {verdict_emoji} **{item.verdict}** — {item.rationale or '—'}")
        if item.fix:
            fix_label = _FIX_LABEL_BY_CODE.get(code, "Suggested fix")
            lines.append(f"- **{fix_label}**: {item.fix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _label_for_item(item_id: str, code: str, fallback_question: str) -> str:
    if code == "en" and item_id in _ITEM_LABELS_EN:
        return _ITEM_LABELS_EN[item_id]
    if code == "ja" and item_id in _ITEM_LABELS_JA:
        return _ITEM_LABELS_JA[item_id]
    return fallback_question


def report_to_dict(report: SelfCheckReport) -> dict[str, object]:
    return {
        "overall_verdict": report.overall_verdict,
        "counts": {
            "pass": report.pass_count,
            "warn": report.warn_count,
            "fail": report.fail_count,
            "incomplete": sum(1 for i in report.items if i.verdict == "incomplete"),
        },
        "items": [
            {
                "item_id": item.item_id,
                "question": item.question,
                "verdict": item.verdict,
                "rationale": item.rationale,
                "fix": item.fix,
            }
            for item in report.items
        ],
    }


__all__ = [
    "SELF_CHECK_ITEMS",
    "SelfCheckItem",
    "SelfCheckReport",
    "render_self_check_markdown",
    "report_to_dict",
    "run_self_check",
]
