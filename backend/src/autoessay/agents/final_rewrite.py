"""Final global rewrite agent and post-rewrite compliance gate."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import BaseModel, Field, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents import drafter
from autoessay.agents._evidence_policy import EvidencePolicies
from autoessay.agents._language import language_directive
from autoessay.agents.drafter import DraftedSection
from autoessay.agents.phase_context import phase_context_prompt_block
from autoessay.config import Settings, get_settings
from autoessay.db import SessionLocal
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

EvidenceStatus = Literal["source_bound", "model_backed"]
CONTROLLED_POLISH_NO_SCORE_GAIN_PATIENCE = 2

if TYPE_CHECKING:
    from autoessay.agents._critic_polish_loop import ExpertCritiqueOutput


class ApprovedTargetState(TypedDict):
    approved_blocker_high_count: int
    remaining_approved_count: int
    remaining_approved_targets: list[dict[str, object]]
    cleared_approved_count: int
    cleared_approved_targets: list[dict[str, object]]
    remaining_blocker_high_count: int
    remaining_blocker_high_targets: list[dict[str, object]]
    cleared_blocker_high_count: int
    cleared_blocker_high_targets: list[dict[str, object]]
    critic_error_count: int
    critic_errors: list[dict[str, object]]


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


FINAL_REWRITE_SYSTEM_PROMPT = """你是中文学术论文 final-rewrite editor。
在 stylist 输出基础上完成结构性收尾。

允许：
- 在不改变 citation-bearing 段落证据关系的前提下微调段落顺序；凡含 [N] 的段落，
  必须保留该段原有 citation marker 的编号与出现次数
- 压缩重复论点，但不得合并、删除或拆散 citation-bearing 段落，不得合并、删除或
  重命名 claim_map 条目
- intro / conclusion 加全局 thesis 句
- 规范化过渡词与术语
- 清理编辑脚手架（例如 <a id="..."></a> HTML anchor），但保留可见 markdown 标题
- 对中文稿保留并规范 ## 摘要 / ## 关键词 / ## 参考文献；
  若关键词缺失，可从题名与正文术语中补 3-6 个短关键词
- 做一次全局学术质量收束：去掉跨节重复推进，避免连续多节反复使用同一句式或同一
  保留语；让每一节承担不同功能，并把引言、文献综述、方法、三个经验/案例节、
  讨论、结论之间的递进关系写清楚
- 明确呈现论文的新问题、新材料、新视角、新方法、新论证五类创新来源中已经由原稿
  支持的类别；特别是在引言/文献综述中把研究问题从既有问法改写为何种新问法，
  但不得新增原稿外事实
- 将“证据边界/材料限制”集中到方法、讨论或结论中的合适位置，避免在多个案例节
  机械重复同一句“目录只是入口/不能单独裁定节点”
- 改写模板化、翻译腔、口号式过渡和自我循环句；使用紧凑、明确、有人类学术作者
  判断力度的中文表达

绝对禁止：
- 新增任何 [N] 之外的 citation
- 修改 [N] 编号或 multiset
- 引入原稿未出现的命名实体 / 年份 / 书名 / 作者 / 统计数字 / 因果断言
- 删除已有必要 citation marker
- 把 analytic claim 伪装成 source-backed claim（即不能给 model-backed 段加 [N]）
- 摘要不得以省略号或半截句结尾；必须以完整句号/问号/叹号收束
- 不得把原稿中的证据限制、档案缺口、保留语气改写成已经掌握一手档案、
  已经重建完整档案链、或已经锁定唯一失效节点/月份的定论

输出 JSON：{"manuscript": "...", "claim_map": [...]}
manuscript: full markdown 包含原 [N] markers
claim_map: 与 drafter 同 schema，每条含 paragraph_id / claim_text / source_ids /
evidence_status (source_bound | model_backed)
"""


HOLISTIC_FINAL_REWRITE_SYSTEM_PROMPT = """你是中文学术论文 holistic final-rewrite editor。
你的任务是把 stylist 后的整篇论文一次性改写为同一种中文学术语态，
消除多阶段写作留下的跨节接缝、重复句式、模板化保留语和段落之间的突兀过渡。

你只能做 prose-level rewrite。必须保持论文的证据结构、论点边界和引用结构完全不变。

硬规则：
- 不改变 markdown 非空段落数量；每一个输入段落必须对应一个输出段落
- 不新增、删除、重排、替换或合并任何 [N] citation marker
- 每个段落内的 [N] marker 数量和顺序必须与输入同段完全一致
- 不新增任何命名实体、年份、书名、作者、统计数字、档案名或因果断言
- 不改变论文核心论点，不把保留判断改成定论，不把证据不足改成证据充分
- 不改变证据来源，不改参考文献块，不改参考文献编号
- 不新增、不删除、不重命名标题；保留原来的章节顺序
- 不输出 claim_map；claim_map 将由系统沿用原始版本

允许：
- 在同一个段落内部重写句子，让语气更像稳定的人类中文学术作者
- 删除同义重复、口号式过渡、模板化保留语，但不得删除该段承担的论证功能
- 增强段落之间的承接句和指代清晰度，但不得改变段落数量
- 让引言、文献综述、方法、案例/证据分析、讨论、结论之间的递进关系更连贯
- 保持并适度统一术语，但不得引入原稿没有的新事实

输出 JSON：{"manuscript": "..."}
manuscript 必须是完整 markdown，包含输入中所有 [N] markers，且每个段落的 marker 序列完全一致。
"""


CONTROLLED_POLISH_REWRITE_SYSTEM_PROMPT = """你是中文学术论文 controlled-polish editor。
你在 final_rewrite 已经完成之后工作，只能按专家评审给出的 BLOCKER/HIGH
最小 scope 做定向 prose polish；这是质量收束，不是新研究、不是合规整理。

驱动范围：
- 只允许针对 compliance 与 completeness 问题改写。
- novelty 只作为防回退评分；不得为了提高 novelty 新增材料、事实或夸大断言。
- 每轮只处理输入 target_revision_items 的最小 scope；当存在 BLOCKER/HIGH 时优先修复
  这些关键项，没有关键项但 critic 仍要求修改时，才处理剩余 revision_items。
- 不得整篇 sweeping rewrite。

硬规则：
- 输出完整 markdown，但只改 critic 标出来的 target_revision_items 最小 scope。
- 优先修复专家指出的合规性、完整性和论证收束问题；不要为 novelty 新增材料。
- 保持现有引用和事实边界，避免主动新增 source_id、作者名、出版年份、书名、
  统计数字、档案名或因果断言；如必须调整表述，用更保守、更可审查的说法。
- 不新增参考文献，不改参考文献编号，不改 claim_map 语义。
- 不输出 TODO、[UNCITED]、未解析 citation marker 或编辑说明。

empirical_preservation_guard（适用于含 LaTeX / 表 / 占位符的输入 manuscript）：
- 保留输入 manuscript 中所有 LaTeX 公式块（$$...$$、$...$、
  \begin{equation}...\end{equation}）verbatim；不要 paraphrase 成散文，
  不要删除公式。
- 保留所有 markdown 表格（| ... | 行 或对齐的 ASCII 表）verbatim；
  不要展开成散文段落，不要删除表格。
- 保留所有【待填】、【TBD】、【待补】、[FILL] 等明显占位符 verbatim。
  占位符是 editorial scaffolding，不是 citation / source_id / bibliography entry，
  也不是已经成立的事实断言；**不要把占位符填充成具体数字、
  人名年份、引用编号或新增 claim**。只有 target_revision_items 明确指出
  某占位符可填、且填入内容有已 approved 的 source 支撑时，才可填充。
- 如果原文段落形如实证结论（"研究表明X""结果显示Y""数据证实Z"）但无对应
  表格、引用或【待填】占位支撑，必须改写为"理论预期X""若实证检验支持X"
  "【待填：X的回归结果】"或类似保守表述；这是降级伪造结论，不算新增 claim。

输出 JSON：{"manuscript": "..."}
manuscript 必须是完整 markdown。
"""


def _final_rewrite_system_prompt(policies: EvidencePolicies) -> str:
    blocks = [
        FINAL_REWRITE_SYSTEM_PROMPT,
        policies.section_directive_prefix(),
    ]
    if policies.whitelist_directive:
        blocks.append(policies.whitelist_directive)
    return "\n\n".join(blocks)


def _baseline_as_evidence_test_rewrite_directive(run_dir: Path) -> str:
    settings = get_settings()
    if not settings.baseline_as_evidence_test:
        return ""
    shortlist_path = run_dir / "sources" / "shortlist.json"
    try:
        records = json.loads(shortlist_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        records = []
    if not isinstance(records, list) or not any(
        isinstance(record, dict) and record.get("source_id") == "shadow_baseline_v001"
        for record in records
    ):
        return ""
    return (
        "\n\nbaseline_as_evidence_test_rewrite_directive: "
        "AUTOESSAY_BASELINE_AS_EVIDENCE_TEST is enabled and "
        "shadow_baseline_v001 is a legal approved TEST source. "
        "Do not write the literal source_id 'shadow_baseline_v001' as a "
        "prose noun or author name; keep it only in claim_map.source_ids "
        "metadata and use normal numbered citation markers in the manuscript. "
        "Treat baseline-derived passages like any other approved source "
        "material: preserve supported claims only when the claim_map source "
        "ids still ground them, paraphrase rather than copying baseline "
        "wording, and do not add unsupported claims. The anti-plagiarism "
        "n-gram gate still applies."
    )


class RewriteClaim(BaseModel):
    paragraph_id: StrictStr
    claim_text: StrictStr
    source_ids: list[StrictStr] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "source_bound"

    @validator("paragraph_id", "claim_text")
    def _must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return value

    class Config:
        extra = "ignore"


class FinalRewriteOutput(BaseModel):
    manuscript: StrictStr
    claim_map: list[RewriteClaim]

    @validator("manuscript")
    def _manuscript_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manuscript must be non-empty markdown")
        return value

    @validator("claim_map")
    def _claim_map_must_not_be_empty(cls, value: list[RewriteClaim]) -> list[RewriteClaim]:
        if not value:
            raise ValueError("claim_map must contain at least one claim")
        return value

    class Config:
        extra = "ignore"


class HolisticRewriteOutput(BaseModel):
    manuscript: StrictStr

    @validator("manuscript")
    def _manuscript_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manuscript must be non-empty markdown")
        return value

    class Config:
        extra = "ignore"


class ControlledPolishRewriteOutput(BaseModel):
    manuscript: StrictStr

    @validator("manuscript")
    def _manuscript_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manuscript must be non-empty markdown")
        return value

    class Config:
        extra = "ignore"


@dataclass(frozen=True)
class ComplianceResult:
    failed: bool
    reason: str | None = None
    details: list[dict[str, object]] | None = None

    @classmethod
    def pass_(cls) -> ComplianceResult:
        return cls(failed=False, reason=None, details=[])

    @classmethod
    def fail(
        cls,
        reason: str,
        details: Sequence[Mapping[str, object]] | None = None,
    ) -> ComplianceResult:
        return cls(
            failed=True,
            reason=reason,
            details=[dict(item) for item in (details or [])],
        )


@dataclass(frozen=True)
class RewriteArtifact:
    version: str
    path: Path
    manuscript: str
    claim_map: list[dict[str, object]]
    audit: dict[str, object]


@dataclass(frozen=True)
class ControlledPolishValidation:
    passed: bool
    reasons: list[str]
    details: list[dict[str, object]]


def _validate_polish_candidate_compliance(
    *,
    candidate: Mapping[str, object],
    incumbent: Mapping[str, object],
    root_original: Mapping[str, object],
    settings: Settings,
    run_dir: Path,
    project: Project,
    session: Session,
    baseline_md: str,
    policies: EvidencePolicies,
) -> ComplianceResult:
    """PR-368 P1-3: thin wrapper around ``_validate_controlled_polish_candidate``
    that returns a ``ComplianceResult`` instead of a ``ControlledPolishValidation``.

    Used by the polish loop and critic-loop replacement paths so that
    round-0 / accepted-polish / critic-loop outputs all go through the
    same deterministic compliance check before being committed as the
    rewritten manuscript. The narrow contract: validates the manuscript
    + claim_map that the caller is about to write / export.

    codex AGREE-WITH-AMENDMENTS PR-368: do NOT use
    ``_prepare_rewrite_for_compliance`` as a validator — it does
    normalization, not validation.
    """
    validation = _validate_controlled_polish_candidate(
        candidate=candidate,
        incumbent=incumbent,
        root_original=root_original,
        settings=settings,
        run_dir=run_dir,
        project=project,
        session=session,
        baseline_md=baseline_md,
        policies=policies,
    )
    if validation.passed:
        return ComplianceResult.pass_()
    return ComplianceResult.fail(
        reason=";".join(validation.reasons) or "polish_compliance_failed",
        details=validation.details,
    )


def run_final_rewrite(
    run_id: str,
    session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Final global rewrite + post-rewrite compliance.

    Reads stylist manuscript + drafter claim_map. The LLM may reorganize
    prose, then the adapter re-validates against existing citation /
    grounding hooks and writes ``rewrite/vNNN`` artifacts. On success this
    phase lands at ``CRITIC_RUNNING``; the API/worker wrapper starts critic.
    """

    from autoessay.phase_lock import phase_lock_release_on_exit

    with phase_lock_release_on_exit(run_id, "rewrite", lock_token, session=session):
        if session is not None:
            return _run_final_rewrite_with_session(run_id, session, hooks or HookRegistry())
        with SessionLocal() as owned:
            return _run_final_rewrite_with_session(run_id, owned, hooks or HookRegistry())


def run_final_rewrite_then_critic(
    run_id: str,
    session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run final rewrite and, if it passes, continue into critic.

    The active phase lock is held across both steps so the user click is
    still one serialized critical section.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit

    def _execute(active_session: Session) -> dict[str, object]:
        rewrite_result = run_final_rewrite(run_id, active_session, hooks or HookRegistry())
        run = active_session.scalar(select(Run).where(Run.id == run_id))
        if run is None or run.state != "CRITIC_RUNNING":
            return rewrite_result
        if lock_token is not None:
            from autoessay.phase_lock import transfer_phase_lock

            if not transfer_phase_lock(
                active_session,
                run,
                "final_rewrite",
                "critic",
                lock_token,
            ):
                append_event(
                    active_session,
                    run,
                    "phase_lock_transfer_failed",
                    {
                        "from_phase": "final_rewrite",
                        "to_phase": "critic",
                        "run_id": run.id,
                    },
                )
                active_session.commit()
                return rewrite_result
            active_session.commit()
        from autoessay.agents.critic import run_critic

        return run_critic(
            run_id,
            active_session,
            hooks=hooks or HookRegistry(),
            lock_token=lock_token,
        )

    with phase_lock_release_on_exit(run_id, "final_rewrite", lock_token, session=session):
        if session is not None:
            return _execute(session)
        with SessionLocal() as owned:
            return _execute(owned)


def latest_rewrite_dir(run_dir: str | Path) -> Path | None:
    rewrite_root = Path(run_dir) / "rewrite"
    if not rewrite_root.exists():
        return None
    candidates = [
        path
        for path in rewrite_root.glob("v[0-9][0-9][0-9]")
        if path.is_dir() and (path / "manuscript.md").is_file()
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.name)[-1]


def load_latest_rewrite_artifact(run_dir: str | Path) -> RewriteArtifact | None:
    rewrite_dir = latest_rewrite_dir(run_dir)
    if rewrite_dir is None:
        return None
    manuscript = _read_optional_text(rewrite_dir / "manuscript.md")
    if not manuscript.strip():
        return None
    claim_map = _load_json_array_of_objects(rewrite_dir / "claim_map.json")
    audit = _load_json_mapping(rewrite_dir / "audit.json")
    return RewriteArtifact(
        version=rewrite_dir.name,
        path=rewrite_dir,
        manuscript=manuscript,
        claim_map=claim_map,
        audit=audit,
    )


def complete_downstream_review_fallback(
    run: Run,
    session: Session,
    *,
    previous_rewrite: RewriteArtifact,
    blockers: Sequence[object],
    draft_version: str,
    reason: str,
) -> RewriteArtifact:
    """Reject a rewrite after downstream review and expose stylist fallback.

    Final rewrite has its own structural compliance checks, but a rewrite
    candidate can still fail the full critic/citation audit. In that case
    write a new latest rewrite artifact containing the original stylist
    manuscript, preserving the rejected rewrite and blocker payloads for audit.
    """

    run_dir = Path(run.run_dir)
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        raise ValueError("downstream rewrite fallback needs a completed styled draft")
    original = _load_original_payload(draft_dir)
    settings = get_settings()
    if settings.baseline_as_evidence_test:
        original["manuscript"] = drafter._sanitize_baseline_as_evidence_source_mentions(
            str(original["manuscript"])
        )
    fallback, original_citation_repair = _maybe_pre_repair_numeric_citations(
        original,
        run_dir=run_dir,
    )
    rewrite_dir = _next_rewrite_dir(run_dir)
    rewrite_dir.mkdir(parents=True, exist_ok=False)
    summary = _diff_summary(
        previous_rewrite.manuscript,
        str(fallback.get("manuscript") or ""),
        previous_rewrite.claim_map,
        list(fallback.get("claim_map") or []),
    )
    blocker_payloads = [_downstream_blocker_payload(blocker) for blocker in blockers]
    audit_payload: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "draft_version": draft_version,
        "rewrite_version": rewrite_dir.name,
        "rewrite_mode": "holistic" if settings.final_rewrite_holistic else "global",
        "compliance": {
            "failed": False,
            "reason": None,
            "details": [],
        },
        "rewrite_diff_summary": summary,
        "fallback_to_original": True,
        "fallback_reason": reason,
        "downstream_rejected_rewrite_version": previous_rewrite.version,
        "downstream_blockers": blocker_payloads,
        "accepted_diff_summary": summary,
        "rejected_manuscript_path": f"rewrite/{rewrite_dir.name}/rejected_manuscript.md",
    }
    if original_citation_repair.get("changed"):
        audit_payload["citation_pre_repair"] = {
            "original": original_citation_repair,
            "rewritten": {
                "changed": False,
                "unresolved_before": [],
                "unresolved_after": [],
            },
        }
    _write_text(rewrite_dir / "rejected_manuscript.md", previous_rewrite.manuscript)
    _write_json(rewrite_dir / "rejected_claim_map.json", previous_rewrite.claim_map)
    _write_text(rewrite_dir / "manuscript.md", str(fallback.get("manuscript") or ""))
    _write_json(rewrite_dir / "claim_map.json", list(fallback.get("claim_map") or []))
    _write_text(
        rewrite_dir / "diff.txt",
        _unified_diff(previous_rewrite.manuscript, str(fallback.get("manuscript") or "")),
    )
    _write_json(rewrite_dir / "audit.json", audit_payload)
    append_event(
        session,
        run,
        "rewrite_policy_fallback",
        {
            "phase": "final_rewrite",
            "draft_version": draft_version,
            "rewrite_version": rewrite_dir.name,
            "reason": reason,
            "details": blocker_payloads,
            "rejected_rewrite_version": previous_rewrite.version,
        },
    )
    append_event(
        session,
        run,
        "downstream_rewrite_fallback",
        {
            "phase": "critic",
            "draft_version": draft_version,
            "rewrite_version": rewrite_dir.name,
            "rejected_rewrite_version": previous_rewrite.version,
            "blocker_count": len(blocker_payloads),
            "reason": reason,
        },
    )
    session.commit()
    fallback_artifact = load_latest_rewrite_artifact(run_dir)
    if fallback_artifact is None:
        raise ValueError("downstream rewrite fallback did not write a readable artifact")
    return fallback_artifact


def _downstream_blocker_payload(blocker: object) -> dict[str, object]:
    if hasattr(blocker, "dict"):
        payload = blocker.dict()
        if isinstance(payload, dict):
            return dict(payload)
    if isinstance(blocker, Mapping):
        return dict(blocker)
    return {"description": str(blocker)}


def rewrite_summary_for_run(run: Run) -> dict[str, object] | None:
    return rewrite_summary_for_run_dir(Path(run.run_dir))


def rewrite_summary_for_run_dir(run_dir: str | Path) -> dict[str, object] | None:
    artifact = load_latest_rewrite_artifact(run_dir)
    if artifact is None:
        return None
    summary = artifact.audit.get("rewrite_diff_summary")
    if not isinstance(summary, dict):
        summary = _diff_summary("", artifact.manuscript, [], artifact.claim_map)
    return {
        "rewrite_version": artifact.version,
        "rewrite_audit_path": f"rewrite/{artifact.version}/audit.json",
        "rewrite_diff_summary": {
            key: value for key, value in dict(summary).items() if isinstance(key, str)
        },
    }


def latest_rewrite_summary(run_dir: str | Path) -> dict[str, object] | None:
    return rewrite_summary_for_run_dir(run_dir)


def attempt_exports_policy_polish_retry(
    *,
    run: Run,
    project: Project,
    session: Session,
    guidance: str,
    failure_class: str,
    retry_index: int,
    hooks: HookRegistry | None = None,
    audit_rows: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Use the final-rewrite polish executor to repair an exports policy miss.

    The retry writes a new rewrite/vNNN artifact and leaves export success/fail
    to the caller's next export-gate pass. It never transitions run state.
    """

    settings = get_settings()
    run_dir = Path(run.run_dir)
    artifact = load_latest_rewrite_artifact(run_dir)
    if artifact is None:
        return {"status": "skipped_no_rewrite_artifact"}
    if settings.final_rewrite_stub or settings.critic_stub:
        return {"status": "skipped_stub_mode"}
    target_item = {
        "id": f"EXPORTS_POLICY_RETRY_{retry_index}",
        "issue": guidance,
        "scope": "exports_policy_gate",
        "original_text_anchor": "exports policy failure",
        "issue_type": "OVERCLAIM",
        "severity": "BLOCKER",
        "why_it_matters": "Exports policy gate blocked release of the final manuscript.",
        "suggestion": (
            "Fix the policy failure without adding unsupported facts, sources, "
            "authors, years, statistics, archives, or causal claims."
        ),
        "expected_output_after_revision": (
            "A full manuscript whose claims are downgraded or clarified enough "
            "for the exports policy gate to pass."
        ),
        "acceptance_test": guidance,
        "later_review_rule": "Exports will rerun the policy gate after this retry.",
        "failure_class": failure_class,
    }
    critique = SimpleNamespace(
        scores=None,
        deletion_or_compression_plan=[],
    )
    candidate = _controlled_polish_rewrite_via_harness(
        manuscript=artifact.manuscript,
        critique=critique,
        target_items=[target_item],
        run=run,
        project=project,
        session=session,
        hooks=hooks or HookRegistry(),
        rewrite_version=f"{artifact.version}_exports_retry{retry_index}",
        attempt=retry_index,
        policies=EvidencePolicies.from_settings("final", settings),
    )
    if not candidate:
        return {"status": "rewrite_failed"}

    rewrite_dir = _next_rewrite_dir(run_dir)
    rewrite_dir.mkdir(parents=True, exist_ok=False)
    claim_map = [dict(item) for item in artifact.claim_map]
    summary = _diff_summary(
        artifact.manuscript,
        candidate,
        artifact.claim_map,
        claim_map,
    )
    audit_payload: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rewrite_version": rewrite_dir.name,
        "rewrite_mode": "exports_policy_auto_retry",
        "source_rewrite_version": artifact.version,
        "exports_policy_retry": {
            "retry_index": retry_index,
            "failure_class": failure_class,
            "guidance": guidance,
            "target_revision_item": target_item,
            "claim_audit": [dict(row) for row in audit_rows],
        },
        "compliance": {
            "failed": False,
            "reason": None,
            "details": [],
        },
        "rewrite_diff_summary": summary,
        "accepted_diff_summary": summary,
    }
    _write_text(rewrite_dir / "manuscript.md", candidate)
    _write_json(rewrite_dir / "claim_map.json", claim_map)
    _write_text(rewrite_dir / "diff.txt", _unified_diff(artifact.manuscript, candidate))
    _write_json(rewrite_dir / "audit.json", audit_payload)
    append_event(
        session,
        run,
        "exports_policy_polish_retry",
        {
            "phase": "exports",
            "retry_index": retry_index,
            "rewrite_version": rewrite_dir.name,
            "source_rewrite_version": artifact.version,
            "failure_class": failure_class,
            "guidance": guidance,
        },
    )
    session.commit()
    return {
        "status": "rewritten",
        "rewrite_version": rewrite_dir.name,
        "source_rewrite_version": artifact.version,
        "diff_summary": summary,
    }


def _run_final_rewrite_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"USER_REVISION_REVIEW", "REWRITE_RUNNING"}:
        raise InvalidTransition(
            f"Final rewrite requires USER_REVISION_REVIEW or REWRITE_RUNNING, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    run_dir = Path(run.run_dir)
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        return _fail(
            run,
            session,
            "FAILED_FIXABLE",
            "Final rewrite needs a completed styled draft.",
            failure_class="failed_fixable",
        )
    original = _load_original_payload(draft_dir)
    settings = get_settings()
    if settings.baseline_as_evidence_test:
        original["manuscript"] = drafter._sanitize_baseline_as_evidence_source_mentions(
            str(original["manuscript"])
        )
    if not original["manuscript"].strip() or not original["claim_map"]:
        return _fail(
            run,
            session,
            "FAILED_FIXABLE",
            "Final rewrite found missing stylist manuscript or claim_map.",
            failure_class="failed_fixable",
        )

    policies = EvidencePolicies.from_settings("final", settings)
    if run.state == "USER_REVISION_REVIEW":
        transition(run, "REWRITE_RUNNING", session, reason="Final rewrite started")
    # PR-369 X-2 (codex review): snapshot ``mathematical_mode`` into
    # the ``phase_started`` event so the audit trail captures the
    # value the rewriter is about to act on. Future polish/critic
    # round-0 decisions should source from this snapshot rather than
    # re-reading ``run.mathematical_mode`` on the fly, so a mid-phase
    # flip can never produce a half-on / half-off audit.
    append_event(
        session,
        run,
        "phase_started",
        {
            "phase": "final_rewrite",
            "run_id": run.id,
            "draft_version": draft_dir.name,
            "mathematical_mode_snapshot": bool(getattr(run, "mathematical_mode", False)),
        },
    )
    append_event(
        session,
        run,
        "evidence_policy_applied",
        {"phase": "final_rewrite", "run_id": run.id, **policies.event_payload()},
    )
    session.commit()
    session.refresh(run)
    assert_run_active(run, session)

    rewrite_dir = _next_rewrite_dir(run_dir)
    rewrite_dir.mkdir(parents=True, exist_ok=False)
    if settings.final_rewrite_stub:
        _write_text(rewrite_dir / "manuscript.md", original["manuscript"])
        _write_json(rewrite_dir / "claim_map.json", original["claim_map"])
        _write_text(rewrite_dir / "diff.txt", "")
        _write_json(rewrite_dir / "audit.json", {})
        summary = _diff_summary(
            original["manuscript"],
            original["manuscript"],
            original["claim_map"],
            original["claim_map"],
        )
        return _complete_success(
            run=run,
            session=session,
            rewrite_dir=rewrite_dir,
            summary=summary,
            compliance=ComplianceResult.pass_(),
            draft_version=draft_dir.name,
            stub=True,
        )

    try:
        raw_rewritten = _final_rewrite_via_harness(
            original=original,
            run=run,
            project=project,
            hooks=hooks,
            session=session,
            rewrite_version=rewrite_dir.name,
            policies=policies,
        )
    except (SchemaViolationError, TimeoutError, OSError, RuntimeError) as exc:
        return _complete_original_fallback_after_llm_error(
            run,
            session,
            rewrite_dir=rewrite_dir,
            original=original,
            draft_version=draft_dir.name,
            settings=settings,
            run_dir=run_dir,
            error=exc,
        )
    except Exception as exc:  # noqa: BLE001 - optional rewrite falls back on provider errors.
        return _complete_original_fallback_after_llm_error(
            run,
            session,
            rewrite_dir=rewrite_dir,
            original=original,
            draft_version=draft_dir.name,
            settings=settings,
            run_dir=run_dir,
            error=exc,
        )
    (
        rewritten,
        fallback_original,
        compliance,
        summary,
        citation_pre_repair,
        material_scope_calibration,
    ) = _prepare_rewrite_for_compliance(
        raw_rewritten,
        original=original,
        settings=settings,
        run_dir=run_dir,
        policies=policies,
        run=run,
        session=session,
        project=project,
    )
    citation_retry_audit: dict[str, object] | None = None
    if compliance.failed and compliance.reason == "cite_marker_multiset_change":
        first_reason = compliance.reason
        first_summary = summary
        try:
            raw_retry = _final_rewrite_via_harness(
                original=original,
                run=run,
                project=project,
                hooks=hooks,
                session=session,
                rewrite_version=rewrite_dir.name,
                policies=policies,
                retry_guidance=_citation_multiset_retry_guidance(original["manuscript"]),
                attempt=2,
            )
            (
                retry_rewritten,
                retry_fallback_original,
                retry_compliance,
                retry_summary,
                retry_citation_pre_repair,
                retry_material_scope_calibration,
            ) = _prepare_rewrite_for_compliance(
                raw_retry,
                original=original,
                settings=settings,
                run_dir=run_dir,
                policies=policies,
                run=run,
                session=session,
                project=project,
            )
            citation_retry_audit = {
                "attempted": True,
                "first_failure_reason": first_reason,
                "first_rewrite_diff_summary": first_summary,
                "retry_compliance_failed": retry_compliance.failed,
                "retry_failure_reason": retry_compliance.reason,
            }
            rewritten = retry_rewritten
            fallback_original = retry_fallback_original
            compliance = retry_compliance
            summary = retry_summary
            citation_pre_repair = retry_citation_pre_repair
            material_scope_calibration = retry_material_scope_calibration
        except (SchemaViolationError, TimeoutError, OSError, RuntimeError) as exc:
            citation_retry_audit = {
                "attempted": True,
                "first_failure_reason": first_reason,
                "first_rewrite_diff_summary": first_summary,
                "retry_error": type(exc).__name__,
            }
        except Exception as exc:  # noqa: BLE001 - retry-side vendor boundary matches first attempt.
            citation_retry_audit = {
                "attempted": True,
                "first_failure_reason": first_reason,
                "first_rewrite_diff_summary": first_summary,
                "retry_error": type(exc).__name__,
            }
    controlled_polish_audit: dict[str, object] | None = None
    if not compliance.failed:
        (
            rewritten,
            compliance,
            summary,
            controlled_polish_audit,
        ) = _maybe_run_controlled_polish_loop(
            rewritten=rewritten,
            original=original,
            current_summary=summary,
            settings=settings,
            run_dir=run_dir,
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            rewrite_version=rewrite_dir.name,
            draft_version=draft_dir.name,
            policies=policies,
        )
    audit_payload: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "draft_version": draft_dir.name,
        "rewrite_version": rewrite_dir.name,
        "rewrite_mode": "holistic" if settings.final_rewrite_holistic else "global",
        "compliance": {
            "failed": compliance.failed,
            "reason": compliance.reason,
            "details": compliance.details or [],
        },
        "rewrite_diff_summary": summary,
    }
    if citation_pre_repair["original"].get("changed") or citation_pre_repair["rewritten"].get(
        "changed"
    ):
        audit_payload["citation_pre_repair"] = citation_pre_repair
    if material_scope_calibration.get("changed"):
        audit_payload["material_scope_calibration"] = material_scope_calibration
    if citation_retry_audit is not None:
        audit_payload["citation_multiset_retry"] = citation_retry_audit
    if controlled_polish_audit is not None:
        audit_payload["controlled_polish_loop"] = controlled_polish_audit
    if compliance.failed:
        rejected = rewritten
        rejected_summary = summary
        rewritten = {
            "manuscript": fallback_original["manuscript"],
            "claim_map": fallback_original["claim_map"],
        }
        summary = _diff_summary(
            original["manuscript"],
            rewritten["manuscript"],
            original["claim_map"],
            rewritten["claim_map"],
        )
        audit_payload["fallback_to_original"] = True
        audit_payload["fallback_reason"] = compliance.reason or "post_rewrite_compliance_failed"
        audit_payload["rejected_rewrite_diff_summary"] = rejected_summary
        audit_payload["rejected_manuscript_path"] = (
            f"rewrite/{rewrite_dir.name}/rejected_manuscript.md"
        )
        audit_payload["accepted_diff_summary"] = summary
        _write_text(rewrite_dir / "rejected_manuscript.md", str(rejected["manuscript"]))
        _write_json(rewrite_dir / "rejected_claim_map.json", rejected["claim_map"])
        if settings.polish_loop_enabled:
            (
                rewritten,
                polish_compliance,
                summary,
                controlled_polish_audit,
            ) = _maybe_run_controlled_polish_loop(
                rewritten=rewritten,
                original=original,
                current_summary=summary,
                settings=settings,
                run_dir=run_dir,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                rewrite_version=rewrite_dir.name,
                draft_version=draft_dir.name,
                policies=policies,
            )
            if controlled_polish_audit is not None:
                controlled_polish_audit["input_source"] = (
                    "fallback_original_after_failed_final_rewrite"
                )
                audit_payload["controlled_polish_loop"] = controlled_polish_audit
            if (
                not polish_compliance.failed
                and controlled_polish_audit is not None
                and _as_int(controlled_polish_audit.get("accepted_rewrites")) > 0
            ):
                audit_payload["polish_replaced_fallback"] = True
                audit_payload["accepted_diff_summary"] = summary

    critic_loop_audit = _maybe_run_production_critic_loop(
        rewritten=rewritten,
        original=original,
        summary=summary,
        settings=settings,
        run=run,
        project=project,
        session=session,
        hooks=hooks,
        rewrite_dir=rewrite_dir,
    )
    if critic_loop_audit is not None:
        audit_payload["critic_loop"] = critic_loop_audit
        selected_metrics = critic_loop_audit.get("selected_metrics")
        if isinstance(selected_metrics, Mapping):
            audit_payload["critic_loop_selected_metrics"] = dict(selected_metrics)
        summary = _diff_summary(
            str(original.get("manuscript") or ""),
            str(rewritten["manuscript"]),
            _claim_records(original.get("claim_map")),
            _claim_records(rewritten.get("claim_map")),
        )
        audit_payload["accepted_diff_summary"] = summary

    _write_text(rewrite_dir / "manuscript.md", rewritten["manuscript"])
    _write_json(rewrite_dir / "claim_map.json", rewritten["claim_map"])
    _write_text(
        rewrite_dir / "diff.txt",
        _unified_diff(original["manuscript"], rewritten["manuscript"]),
    )
    _write_json(rewrite_dir / "audit.json", audit_payload)

    if compliance.failed:
        append_event(
            session,
            run,
            "rewrite_policy_fallback",
            {
                "phase": "final_rewrite",
                "draft_version": draft_dir.name,
                "rewrite_version": rewrite_dir.name,
                "reason": compliance.reason or "post_rewrite_compliance_failed",
                "details": compliance.details or [],
            },
        )
        return _complete_success(
            run=run,
            session=session,
            rewrite_dir=rewrite_dir,
            summary=summary,
            compliance=ComplianceResult.pass_(),
            draft_version=draft_dir.name,
            stub=False,
        )
    return _complete_success(
        run=run,
        session=session,
        rewrite_dir=rewrite_dir,
        summary=summary,
        compliance=compliance,
        draft_version=draft_dir.name,
        stub=False,
    )


def _complete_original_fallback_after_llm_error(
    run: Run,
    session: Session,
    *,
    rewrite_dir: Path,
    original: Mapping[str, object],
    draft_version: str,
    settings: Settings,
    run_dir: Path,
    error: BaseException,
) -> dict[str, object]:
    fallback, original_citation_repair = _maybe_pre_repair_numeric_citations(
        original,
        run_dir=run_dir,
    )
    summary = _diff_summary(
        str(original.get("manuscript") or ""),
        str(fallback.get("manuscript") or ""),
        _claim_records(original.get("claim_map")),
        _claim_records(fallback.get("claim_map")),
    )
    reason = f"llm_error:{type(error).__name__}"
    audit_payload: dict[str, object] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "draft_version": draft_version,
        "rewrite_version": rewrite_dir.name,
        "rewrite_mode": "holistic" if settings.final_rewrite_holistic else "global",
        "compliance": {
            "failed": False,
            "reason": None,
            "details": [],
        },
        "rewrite_diff_summary": summary,
        "fallback_to_original": True,
        "fallback_reason": reason,
        "llm_error": {
            "type": type(error).__name__,
            "message": str(error)[:500],
        },
        "accepted_diff_summary": summary,
    }
    if original_citation_repair.get("changed"):
        audit_payload["citation_pre_repair"] = {
            "original": original_citation_repair,
            "rewritten": {
                "changed": False,
                "unresolved_before": [],
                "unresolved_after": [],
            },
        }
    _write_text(rewrite_dir / "manuscript.md", str(fallback.get("manuscript") or ""))
    _write_json(rewrite_dir / "claim_map.json", list(fallback.get("claim_map") or []))
    _write_text(
        rewrite_dir / "diff.txt",
        _unified_diff(
            str(original.get("manuscript") or ""),
            str(fallback.get("manuscript") or ""),
        ),
    )
    _write_json(rewrite_dir / "audit.json", audit_payload)
    append_event(
        session,
        run,
        "rewrite_policy_fallback",
        {
            "phase": "final_rewrite",
            "draft_version": draft_version,
            "rewrite_version": rewrite_dir.name,
            "reason": reason,
            "details": [{"error": str(error)[:500]}],
        },
    )
    return _complete_success(
        run=run,
        session=session,
        rewrite_dir=rewrite_dir,
        summary=summary,
        compliance=ComplianceResult.pass_(),
        draft_version=draft_version,
        stub=False,
    )


def _maybe_run_production_critic_loop(
    *,
    rewritten: dict[str, Any],
    original: Mapping[str, object],
    summary: dict[str, object],
    settings: Settings,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    rewrite_dir: Path,
) -> dict[str, object] | None:
    if not getattr(settings, "critic_loop_enabled", True):
        return None
    try:
        from autoessay.agents.critic_loop import run_production_critic_loop

        result = run_production_critic_loop(
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            manuscript=str(rewritten.get("manuscript") or ""),
            rewrite_dir=rewrite_dir,
            iterations=int(getattr(settings, "critic_loop_iterations", 3)),
        )
    except Exception as exc:  # noqa: BLE001 - production loop is bounded and optional.
        audit = {
            "status": "error",
            "phase": "final_rewrite",
            "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
            "sidecar_failure_non_blocking": True,
        }
        append_event(
            session,
            run,
            "critic_loop_completed",
            {
                "phase": "final_rewrite",
                "status": "error",
                "reason": audit["reason"],
            },
        )
        return audit
    audit = result.audit
    if result.manuscript.strip() and result.manuscript != str(rewritten.get("manuscript") or ""):
        # PR-368 P1-3 (codex AGREE-WITH-AMENDMENTS): critic-loop can
        # replace the rewriter's output; revalidate before committing so
        # a critic-loop manuscript that broke citation multiset / claim
        # map can't silently flow into export. On fail keep
        # ``rewritten["manuscript"]`` as-is. ``baseline_md`` and
        # ``policies`` are not in this function's scope; fall back to
        # ``_validate_controlled_polish_candidate`` with reasonable
        # defaults pulled from settings.
        candidate_payload: dict[str, Any] = {
            "manuscript": result.manuscript,
            "claim_map": list(rewritten.get("claim_map") or []),
        }
        baseline_md_for_check, _baseline_mode = _controlled_polish_baseline_text(
            rewrite_dir.parent.parent,
        )
        policies_for_check = EvidencePolicies.from_settings("final", settings)
        revalidation = _validate_polish_candidate_compliance(
            candidate=candidate_payload,
            incumbent=rewritten,
            root_original=original,
            settings=settings,
            run_dir=rewrite_dir.parent.parent,
            project=project,
            session=session,
            baseline_md=baseline_md_for_check,
            policies=policies_for_check,
        )
        if revalidation.failed:
            audit["selected_replaced_final_rewrite_manuscript"] = False
            audit["critic_loop_replacement_compliance_failed"] = {
                "reason": revalidation.reason,
                "details": revalidation.details,
            }
            audit["selected_diff_summary"] = summary
        else:
            rewritten["manuscript"] = result.manuscript
            audit["selected_replaced_final_rewrite_manuscript"] = True
            audit["selected_diff_summary"] = _diff_summary(
                str(original.get("manuscript") or ""),
                str(rewritten.get("manuscript") or ""),
                _claim_records(original.get("claim_map")),
                _claim_records(rewritten.get("claim_map")),
            )
    else:
        audit["selected_replaced_final_rewrite_manuscript"] = False
        audit["selected_diff_summary"] = summary
    append_event(
        session,
        run,
        "critic_loop_completed",
        {
            "phase": "final_rewrite",
            "status": audit.get("status"),
            "selected_iter": audit.get("selected_iter"),
            "selected_metrics": audit.get("selected_metrics"),
            "blocking": False,
        },
    )
    audit_path = audit.get("audit_path")
    if isinstance(audit_path, str) and audit_path:
        _write_json(Path(run.run_dir) / audit_path, audit)
    session.commit()
    return audit


def _final_rewrite_via_harness(
    *,
    original: dict[str, Any],
    run: Run,
    project: Project,
    hooks: HookRegistry,
    session: Session,
    rewrite_version: str,
    policies: EvidencePolicies,
    retry_guidance: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    settings = get_settings()
    holistic = bool(settings.final_rewrite_holistic)
    accumulated_context = phase_context_prompt_block(run.run_dir, "final_rewrite")
    user_prompt = (
        _holistic_rewrite_user_prompt(
            manuscript=str(original["manuscript"]),
            claim_map=list(original["claim_map"]),
            project=project,
            retry_guidance=retry_guidance,
            accumulated_context=accumulated_context,
        )
        if holistic
        else _rewrite_user_prompt(
            manuscript=str(original["manuscript"]),
            claim_map=list(original["claim_map"]),
            project=project,
            retry_guidance=retry_guidance,
            accumulated_context=accumulated_context,
        )
    )
    baseline_as_evidence_directive = _baseline_as_evidence_test_rewrite_directive(Path(run.run_dir))
    material_scope_directive = drafter._material_scope_guard_directive(
        drafter._load_material_diagnostic(Path(run.run_dir)),
        selected_thesis=_selected_thesis_for_material_scope(Path(run.run_dir)),
        research_kernel=getattr(run, "research_kernel_json", None),
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    (
                        _holistic_final_rewrite_system_prompt(policies)
                        if holistic
                        else _final_rewrite_system_prompt(policies)
                    )
                    + " "
                    + language_directive(project.language)
                    + baseline_as_evidence_directive
                    + material_scope_directive
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        model=settings.one_api_model,
        temperature=0.1,
        max_tokens=12000 if holistic else 8000,
        response_format={"type": "json_object"},
        request_id=f"final_rewrite_{rewrite_version}_a{attempt}",
        prompt_template_id=("final_rewrite.holistic.v1" if holistic else "final_rewrite.global.v1"),
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id="final_rewrite.global",
        user_id=project.user_id,
        attempt=attempt,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "final_rewrite",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "rewrite_version": rewrite_version,
            "claim_count": len(original["claim_map"]),
            "rewrite_mode": "holistic" if holistic else "global",
        },
    )
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hooks,
            context=context,
            output_schema=HolisticRewriteOutput if holistic else FinalRewriteOutput,
            audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="FinalRewrite"),
            max_corrective_retries=1,
            llm_optional=False,
        ),
    )
    if holistic:
        return _holistic_rewrite_output_to_mapping(
            response.parsed,
            original_claim_map=list(original["claim_map"]),
        )
    return _rewrite_output_to_mapping(response.parsed)


def _holistic_final_rewrite_system_prompt(policies: EvidencePolicies) -> str:
    blocks = [
        HOLISTIC_FINAL_REWRITE_SYSTEM_PROMPT,
        policies.section_directive_prefix(),
    ]
    if policies.whitelist_directive:
        blocks.append(policies.whitelist_directive)
    return "\n\n".join(blocks)


def _rewrite_user_prompt(
    *,
    manuscript: str,
    claim_map: list[object],
    project: Project,
    retry_guidance: str | None = None,
    accumulated_context: str = "",
) -> str:
    payload = {
        "project_title": project.title,
        "language": project.language,
        "manuscript": manuscript,
        "claim_map": claim_map,
    }
    if accumulated_context:
        payload["global_context_pack_non_citable"] = accumulated_context
    retry_block = f"\n\n{retry_guidance.strip()}" if retry_guidance else ""
    return (
        "请对以下 stylist 后论文做 final global rewrite。只返回 JSON；不要解释。"
        f"{retry_block}\n\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _holistic_rewrite_user_prompt(
    *,
    manuscript: str,
    claim_map: list[object],
    project: Project,
    retry_guidance: str | None = None,
    accumulated_context: str = "",
) -> str:
    citation_sequence = drafter._extract_inline_citations(manuscript)
    paragraph_marker_sequences = [
        drafter._extract_inline_citations(paragraph) for paragraph in _paragraphs(manuscript)
    ]
    payload = {
        "project_title": project.title,
        "language": project.language,
        "paragraph_count": len(_paragraphs(manuscript)),
        "citation_sequence": citation_sequence,
        "paragraph_marker_sequences": paragraph_marker_sequences,
        "manuscript": manuscript,
        "claim_map_reference_do_not_edit": claim_map,
    }
    if accumulated_context:
        payload["global_context_pack_non_citable"] = accumulated_context
    retry_block = f"\n\n{retry_guidance.strip()}" if retry_guidance else ""
    return (
        "HOLISTIC 模式：请对以下 stylist 后论文做整篇 prose rewrite。"
        "只返回 JSON；不要解释；不要返回 claim_map。"
        f"{retry_block}\n\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _rewrite_output_to_mapping(parsed: object) -> dict[str, Any]:
    if isinstance(parsed, FinalRewriteOutput):
        return {
            "manuscript": parsed.manuscript,
            "claim_map": [dict(claim.dict()) for claim in parsed.claim_map],
        }
    if isinstance(parsed, Mapping):
        try:
            output = FinalRewriteOutput.parse_obj(parsed)
        except ValidationError as exc:
            raise SchemaViolationError(str(exc), []) from exc
        return {
            "manuscript": output.manuscript,
            "claim_map": [dict(claim.dict()) for claim in output.claim_map],
        }
    raise SchemaViolationError("final rewrite output is not a JSON object", [])


def _holistic_rewrite_output_to_mapping(
    parsed: object,
    *,
    original_claim_map: list[object],
) -> dict[str, Any]:
    if isinstance(parsed, HolisticRewriteOutput):
        manuscript = parsed.manuscript
    elif isinstance(parsed, Mapping):
        try:
            output = HolisticRewriteOutput.parse_obj(parsed)
        except ValidationError as exc:
            raise SchemaViolationError(str(exc), []) from exc
        manuscript = output.manuscript
    else:
        raise SchemaViolationError("holistic final rewrite output is not a JSON object", [])
    claim_map = [
        {key: value for key, value in item.items() if isinstance(key, str)}
        for item in original_claim_map
        if isinstance(item, Mapping)
    ]
    return {"manuscript": manuscript, "claim_map": claim_map}


def _prepare_rewrite_for_compliance(
    rewritten: Mapping[str, object],
    *,
    original: Mapping[str, object],
    settings: Settings,
    run_dir: Path,
    policies: EvidencePolicies,
    run: Run,
    session: Session,
    project: Project,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    ComplianceResult,
    dict[str, object],
    dict[str, dict[str, object]],
    dict[str, object],
]:
    prepared: dict[str, Any] = dict(rewritten)
    if settings.final_rewrite_holistic:
        # HOLISTIC mode is intentionally prose-only. The LLM is not
        # allowed to be a claim_map author; keep the stylist/drafter
        # evidence map as the canonical one and validate that the
        # citation surface still aligns with it.
        prepared["claim_map"] = _claim_records(original.get("claim_map"))
    if settings.baseline_as_evidence_test:
        prepared["manuscript"] = drafter._sanitize_baseline_as_evidence_source_mentions(
            str(prepared.get("manuscript") or "")
        )
    prepared, material_scope_calibration = _maybe_apply_material_scope_calibration(
        prepared,
        run_dir=run_dir,
        run=run,
    )
    compliance_original, original_citation_repair = _maybe_pre_repair_numeric_citations(
        original,
        run_dir=run_dir,
    )
    prepared, rewritten_citation_repair = _maybe_pre_repair_numeric_citations(
        prepared,
        run_dir=run_dir,
    )
    compliance = _run_post_rewrite_compliance(
        rewritten=prepared,
        original=compliance_original,
        settings=settings,
        run_dir=run_dir,
        policies=policies,
        run=run,
        session=session,
        project=project,
    )
    summary = _diff_summary(
        str(original["manuscript"]),
        str(prepared["manuscript"]),
        _claim_records(original.get("claim_map")),
        _claim_records(prepared.get("claim_map")),
    )
    citation_pre_repair = {
        "original": original_citation_repair,
        "rewritten": rewritten_citation_repair,
    }
    return (
        prepared,
        compliance_original,
        compliance,
        summary,
        citation_pre_repair,
        material_scope_calibration,
    )


def _maybe_run_controlled_polish_loop(
    *,
    rewritten: dict[str, Any],
    original: Mapping[str, object],
    current_summary: dict[str, object],
    settings: Settings,
    run_dir: Path,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    rewrite_version: str,
    draft_version: str,
    policies: EvidencePolicies,
) -> tuple[dict[str, Any], ComplianceResult, dict[str, object], dict[str, object]]:
    """Run the bounded expert-critique → targeted-rewrite loop.

    The loop is deliberately local to the latest final-rewrite artifact:
    candidate manuscripts replace the incumbent when the expert three-score
    vector is monotonic. The initial expert revision_items are frozen as
    approved_targets; later critic items outside that set are audit-only
    critic_errors and never create new rewrite scope. Hard compliance fallout
    is left to downstream critic / integrity / exports phases.
    The independent blind A/B scoring remains in critic.
    """
    from autoessay.agents._critic_polish_loop import (
        CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES,
        CONTROLLED_POLISH_MAX_ATTEMPTS,
        QualityScoreSet,
    )

    audit: dict[str, object] = {
        "enabled": bool(settings.polish_loop_enabled),
        "status": "not_attempted",
        "max_attempts": CONTROLLED_POLISH_MAX_ATTEMPTS,
        "max_accepted_rewrites": CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES,
        "acceptance_mode": "scores_monotonic_only",
        "targeting_mode": "approved_targets_from_initial_critic",
        "critic_prompt_version": "v2_top_journal_one_shot",
        "no_score_gain_patience": CONTROLLED_POLISH_NO_SCORE_GAIN_PATIENCE,
        "target_score_threshold": 9.5,
        "accepted_rewrites": 0,
        "ran": False,
        "improvement_found": False,
        "attempts": [],
        "critic_error_count": 0,
        "critic_errors": [],
    }
    if not settings.polish_loop_enabled:
        audit["status"] = "skipped_disabled"
        _append_controlled_polish_exit_event(
            session=session,
            run=run,
            draft_version=draft_version,
            rewrite_version=rewrite_version,
            status=str(audit["status"]),
        )
        return rewritten, ComplianceResult.pass_(), current_summary, audit
    if settings.critic_stub:
        audit["status"] = "skipped_critic_stub"
        _append_controlled_polish_exit_event(
            session=session,
            run=run,
            draft_version=draft_version,
            rewrite_version=rewrite_version,
            status=str(audit["status"]),
        )
        return rewritten, ComplianceResult.pass_(), current_summary, audit

    baseline_md, baseline_mode = _controlled_polish_baseline_text(run_dir)
    audit["baseline_mode"] = baseline_mode
    if baseline_mode != "real" or not baseline_md:
        audit["status"] = "skipped_no_real_baseline"
        _append_controlled_polish_exit_event(
            session=session,
            run=run,
            draft_version=draft_version,
            rewrite_version=rewrite_version,
            status=str(audit["status"]),
        )
        return rewritten, ComplianceResult.pass_(), current_summary, audit

    incumbent: dict[str, Any] = {
        "manuscript": str(rewritten.get("manuscript") or ""),
        "claim_map": _claim_records(rewritten.get("claim_map")),
    }
    polish_dir = run_dir / "drafts" / draft_version / "polish"
    current_critique = _controlled_polish_critique_via_harness(
        manuscript=str(incumbent["manuscript"]),
        run=run,
        project=project,
        session=session,
        rewrite_version=rewrite_version,
        attempt=0,
        stage="incumbent",
    )
    if current_critique is None:
        audit["status"] = "critic_failed"
        _append_controlled_polish_exit_event(
            session=session,
            run=run,
            draft_version=draft_version,
            rewrite_version=rewrite_version,
            status=str(audit["status"]),
        )
        return rewritten, ComplianceResult.pass_(), current_summary, audit

    audit["ran"] = True

    # Round 0 — holistic revision (codex AGREE-WITH-AMENDMENTS 2026-05-12).
    # Default OFF + canary; flag flips to ON only after real-paper validation.
    # When ON: take the incumbent critique, ask the LLM to integrate every
    # revision item into one full rewrite, unconditionally accept the result
    # iff it passes a deterministic sanity gate, then re-run the critique on
    # the new incumbent so the structured for-loop below sees post-round0
    # scores / items / approved_targets. ``round0_applied`` is read at the
    # tail of the function to make sure a round-0-only manuscript still
    # lands in ``paper_polished.md`` even when no structured round accepts.
    # Round 0 v2 (2026-05-12): two LLM calls in sequence.
    #   Stage A: incumbent manuscript (input to the loop).
    #   Stage B: open-prompt foundation-model rewrite (gpt-5.5 via existing
    #            one-api gateways, no system prompt, no JSON schema, no
    #            citation hard constraint). This is where the model can
    #            strategically restructure, add canonical references, mark
    #            unsupported empirical claims as 待填, etc.
    #   Stage C: pipeline's existing controlled_polish rewriter on stage B
    #            with the V2 incumbent critique as scope. This re-anchors
    #            stage B's output in pipeline's validators + source pool so
    #            the structured iterations 1+ can keep operating normally.
    # The previous "1 LLM call with V2 critique JSON anchor + JSON output +
    # citation multiset sanity gate" design was scrapped after 2026-05-12
    # canary: the V2 schema + citation hard constraint structurally prevent
    # exactly the strategic moves (delete confabulation / add canonical refs
    # / restructure) that round 0 was supposed to enable.
    # PR-366 (2026-05-13): switched from env flag to per-run
    # ``run.mathematical_mode``. Default false → no round-0 → cheap
    # ~14 min run. User opts in via wizard or workspace checkbox to
    # get gpt-5.5 holistic rewrite. The legacy
    # ``settings.polish_holistic_round0_enabled`` env flag is now
    # ignored; codex PR-366 review insisted execution logic NOT OR
    # with env, otherwise the UI checkbox is meaningless when admin
    # flips env on.
    mathematical_mode_active = bool(getattr(run, "mathematical_mode", False))
    round0_holistic_audit: dict[str, object] = {
        "enabled": mathematical_mode_active,
        "status": "skipped_disabled",
    }
    audit["round0_holistic"] = round0_holistic_audit
    round0_applied = False
    if mathematical_mode_active:
        pre_round0_incumbent_text = str(incumbent["manuscript"])
        round0_holistic_audit["status"] = "attempted"
        round0_holistic_audit["pre_round0_scores"] = quality_score_payload(
            current_critique.scores,
        )
        round0_holistic_audit["pre_round0_top_journal_readiness"] = str(
            getattr(current_critique, "top_journal_readiness", "") or "",
        )
        stage_b_text = _controlled_polish_holistic_round0_open_prompt(
            manuscript=pre_round0_incumbent_text,
            run=run,
            project=project,
            session=session,
            rewrite_version=rewrite_version,
        )
        if stage_b_text is None:
            round0_holistic_audit["status"] = "stage_b_open_prompt_failed_skipped"
        else:
            stage_b_path = polish_dir / "round0_stage_b_open_prompt.md"
            _write_text(stage_b_path, stage_b_text)
            round0_holistic_audit["stage_b_path"] = str(
                stage_b_path.relative_to(run_dir),
            )
            round0_holistic_audit["stage_b_chars"] = len(stage_b_text)
            # Stage C: feed stage B through pipeline's existing V2 rewriter
            # with the incumbent critique as scope. This re-grounds whatever
            # the foundation model produced in pipeline's source pool /
            # citation rules / claim_map invariants so iter 1+ can run.
            approved_targets_for_stage_c = _approved_targets_from_revision_items(
                current_critique.revision_items,
            )
            current_target_state_for_stage_c = _approved_target_state(
                approved_targets_for_stage_c,
                current_critique.revision_items,
            )
            target_items_for_stage_c = list(
                current_target_state_for_stage_c["remaining_approved_targets"],
            )
            stage_c_text = _controlled_polish_rewrite_via_harness(
                manuscript=stage_b_text,
                critique=current_critique,
                target_items=target_items_for_stage_c,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                rewrite_version=rewrite_version,
                attempt=0,
                policies=policies,
            )
            if stage_c_text is None:
                # Stage B was unable to round-trip through pipeline rewriter.
                # Revert to stage A; iter 1 runs as if round 0 never happened.
                round0_holistic_audit["status"] = "stage_c_rewrite_failed_reverted"
            else:
                stage_c_path = polish_dir / "round0_stage_c_pipeline_rewrite.md"
                _write_text(stage_c_path, stage_c_text)
                round0_holistic_audit["stage_c_path"] = str(
                    stage_c_path.relative_to(run_dir),
                )
                round0_holistic_audit["stage_c_chars"] = len(stage_c_text)
                new_critique = _controlled_polish_critique_via_harness(
                    manuscript=stage_c_text,
                    run=run,
                    project=project,
                    session=session,
                    rewrite_version=rewrite_version,
                    attempt=0,
                    stage="incumbent_after_round0",
                )
                if new_critique is None:
                    # Stage C produced output but re-critique failed: accept
                    # stage C anyway (user's "整体轮无条件接受") and keep the
                    # incumbent critique as scope for the for loop. The for
                    # loop will note the mismatch via candidate_critic_failed
                    # patience if items don't anchor cleanly.
                    incumbent["manuscript"] = stage_c_text
                    round0_applied = True
                    round0_holistic_audit["status"] = (
                        "succeeded_but_recritique_failed_using_pre_critique"
                    )
                else:
                    incumbent["manuscript"] = stage_c_text
                    current_critique = new_critique
                    round0_applied = True
                    round0_holistic_audit["status"] = "succeeded"
                    round0_holistic_audit["post_round0_scores"] = quality_score_payload(
                        current_critique.scores,
                    )
                    round0_holistic_audit["post_round0_top_journal_readiness"] = str(
                        getattr(current_critique, "top_journal_readiness", "") or "",
                    )

    incumbent_scores: QualityScoreSet = current_critique.scores
    pre_loop_scores = current_critique.scores
    audit["pre_loop_scores"] = quality_score_payload(pre_loop_scores)
    audit["initial_score_clipped"] = bool(
        getattr(incumbent_scores, "score_clipped", False),
    )
    audit["initial_critic_schema_partial_fields"] = _schema_partial_fields(
        current_critique,
    )
    audit["initial_top_journal_readiness"] = str(
        getattr(current_critique, "top_journal_readiness", "") or "",
    )
    audit["initial_editorial_decision_if_submitted_now"] = str(
        getattr(current_critique, "editorial_decision_if_submitted_now", "") or "",
    )
    handoff_payload = _controlled_polish_handoff_payload(
        current_critique,
        project=project,
        rewrite_version=rewrite_version,
    )
    handoff_path = polish_dir / "polish_handoff_to_compliance_phase.json"
    _write_json(handoff_path, handoff_payload)
    audit["polish_handoff_to_compliance_phase_path"] = str(
        handoff_path.relative_to(run_dir),
    )
    approved_targets = _approved_targets_from_revision_items(current_critique.revision_items)
    current_target_state = _approved_target_state(
        approved_targets,
        current_critique.revision_items,
    )
    audit["approved_targets"] = approved_targets
    audit["approved_target_count"] = len(approved_targets)
    audit["approved_blocker_high_count"] = int(
        current_target_state["approved_blocker_high_count"],
    )
    accepted_rewrites = 0
    consecutive_no_score_gain = 0
    critic_errors_payload = audit["critic_errors"]
    assert isinstance(critic_errors_payload, list)
    attempts_payload = audit["attempts"]
    assert isinstance(attempts_payload, list)
    exit_status: str | None = None

    for attempt in range(1, CONTROLLED_POLISH_MAX_ATTEMPTS + 1):
        high_items = _high_blocker_revision_items(current_critique)
        revision_items = list(current_critique.revision_items)
        target_items = list(current_target_state["remaining_approved_targets"])
        attempt_payload: dict[str, object] = {
            "attempt": attempt,
            "incumbent_scores": quality_score_payload(incumbent_scores),
            "incumbent_needs_revision": current_critique.needs_revision,
            "incumbent_revision_items": _revision_items_payload(
                revision_items,
            ),
            "high_blocker_count": len(high_items),
            "high_blocker_items": _revision_items_payload(high_items),
            "target_revision_items": _revision_items_payload(target_items),
            "approved_targets_remaining_count": int(
                current_target_state["remaining_approved_count"],
            ),
            "approved_blocker_high_remaining_count": int(
                current_target_state["remaining_blocker_high_count"],
            ),
            "approved_blocker_high_cleared_count": int(
                current_target_state["cleared_blocker_high_count"],
            ),
            "exit_decisions": [],
        }
        attempts_payload.append(attempt_payload)
        exit_decisions = attempt_payload["exit_decisions"]
        assert isinstance(exit_decisions, list)

        approved_targets_decision = _approved_targets_exit_decision(current_target_state)
        exit_decisions.append(approved_targets_decision)
        if approved_targets_decision["allowed"]:
            attempt_payload["status"] = "approved_targets_cleared"
            exit_status = "approved_targets_cleared"
            break

        target_score_decision = _target_score_exit_decision(
            incumbent_scores,
            current_target_state,
            9.5,
        )
        exit_decisions.append(target_score_decision)
        if target_score_decision["allowed"]:
            attempt_payload["status"] = "target_score_reached"
            exit_status = "target_score_reached"
            break

        max_accepted_decision = {
            "condition": "max_accepted_rewrites",
            "accepted_rewrites": accepted_rewrites,
            "max_accepted_rewrites": CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES,
            "allowed": accepted_rewrites >= CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES,
        }
        exit_decisions.append(max_accepted_decision)
        if max_accepted_decision["allowed"]:
            attempt_payload["status"] = "max_accepted_rewrites"
            exit_status = "stopped_max_accepted_rewrites"
            break

        candidate_text = _controlled_polish_rewrite_via_harness(
            manuscript=str(incumbent["manuscript"]),
            critique=current_critique,
            target_items=target_items,
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            rewrite_version=rewrite_version,
            attempt=attempt,
            policies=policies,
        )
        if candidate_text is None:
            attempt_payload["status"] = "rewrite_failed"
            consecutive_no_score_gain += 1
            attempt_payload["consecutive_no_score_gain"] = consecutive_no_score_gain
            no_score_gain_decision = _no_score_gain_exit_decision(consecutive_no_score_gain)
            exit_decisions.append(no_score_gain_decision)
            if no_score_gain_decision["allowed"]:
                exit_status = "stopped_no_score_gain"
                break
            continue

        candidate = {
            "manuscript": candidate_text,
            "claim_map": list(incumbent["claim_map"]),
        }
        candidate_critique = _controlled_polish_critique_via_harness(
            manuscript=str(candidate["manuscript"]),
            run=run,
            project=project,
            session=session,
            rewrite_version=rewrite_version,
            attempt=attempt,
            stage="candidate",
        )
        if candidate_critique is None:
            attempt_payload["status"] = "candidate_critic_failed"
            consecutive_no_score_gain += 1
            attempt_payload["consecutive_no_score_gain"] = consecutive_no_score_gain
            no_score_gain_decision = _no_score_gain_exit_decision(consecutive_no_score_gain)
            exit_decisions.append(no_score_gain_decision)
            if no_score_gain_decision["allowed"]:
                exit_status = "stopped_no_score_gain"
                break
            continue

        candidate_scores = candidate_critique.scores
        candidate_high_items = _high_blocker_revision_items(candidate_critique)
        candidate_target_state = _approved_target_state(
            approved_targets,
            candidate_critique.revision_items,
        )
        critic_errors = list(candidate_target_state["critic_errors"])
        if critic_errors:
            for error in critic_errors:
                assert isinstance(error, dict)
                critic_errors_payload.append(
                    {
                        "attempt": attempt,
                        **error,
                    }
                )
        score_monotonic = _scores_monotonic(candidate_scores, incumbent_scores)
        score_gain = _scores_any_gain(candidate_scores, incumbent_scores)
        accepted = score_monotonic
        attempt_payload["candidate_scores"] = quality_score_payload(candidate_scores)
        attempt_payload["candidate_score_clipped"] = bool(
            getattr(candidate_scores, "score_clipped", False),
        )
        attempt_payload["candidate_needs_revision"] = candidate_critique.needs_revision
        attempt_payload["candidate_revision_items"] = _revision_items_payload(
            candidate_critique.revision_items,
        )
        attempt_payload["candidate_schema_partial_fields"] = _schema_partial_fields(
            candidate_critique,
        )
        attempt_payload["candidate_top_journal_readiness"] = str(
            getattr(candidate_critique, "top_journal_readiness", "") or "",
        )
        attempt_payload["candidate_editorial_decision_if_submitted_now"] = str(
            getattr(candidate_critique, "editorial_decision_if_submitted_now", "") or "",
        )
        attempt_payload["candidate_high_blocker_count"] = len(candidate_high_items)
        attempt_payload["candidate_high_blocker_items"] = _revision_items_payload(
            candidate_high_items,
        )
        attempt_payload["approved_targets_remaining_count"] = int(
            current_target_state["remaining_approved_count"],
        )
        attempt_payload["candidate_approved_targets_remaining_count"] = int(
            candidate_target_state["remaining_approved_count"],
        )
        attempt_payload["candidate_approved_blocker_high_remaining_count"] = int(
            candidate_target_state["remaining_blocker_high_count"],
        )
        attempt_payload["candidate_approved_blocker_high_cleared_count"] = int(
            candidate_target_state["cleared_blocker_high_count"],
        )
        attempt_payload["critic_error_count"] = len(critic_errors)
        attempt_payload["critic_errors"] = critic_errors
        attempt_payload["score_gain"] = score_gain
        attempt_payload["accept_conditions"] = {
            "score_monotonic": score_monotonic,
            "accepted": accepted,
        }
        if accepted:
            accepted_rewrites += 1
            incumbent = candidate
            incumbent_scores = candidate_scores
            current_critique = candidate_critique
            current_target_state = candidate_target_state
            attempt_payload["status"] = "accepted"
            consecutive_no_score_gain = 0 if score_gain else consecutive_no_score_gain + 1
            attempt_payload["consecutive_no_score_gain"] = consecutive_no_score_gain
            approved_targets_decision = _approved_targets_exit_decision(current_target_state)
            exit_decisions.append(approved_targets_decision)
            if approved_targets_decision["allowed"]:
                exit_status = "approved_targets_cleared"
                break
            target_score_decision = _target_score_exit_decision(
                incumbent_scores,
                current_target_state,
                9.5,
            )
            exit_decisions.append(target_score_decision)
            if target_score_decision["allowed"]:
                exit_status = "target_score_reached"
                break
            no_score_gain_decision = _no_score_gain_exit_decision(consecutive_no_score_gain)
            exit_decisions.append(no_score_gain_decision)
            if no_score_gain_decision["allowed"]:
                exit_status = "stopped_no_score_gain"
                break
            continue

        attempt_payload["status"] = "candidate_rejected_by_critic_or_score"
        consecutive_no_score_gain = 0 if score_gain else consecutive_no_score_gain + 1
        attempt_payload["consecutive_no_score_gain"] = consecutive_no_score_gain
        no_score_gain_decision = _no_score_gain_exit_decision(consecutive_no_score_gain)
        exit_decisions.append(no_score_gain_decision)
        if no_score_gain_decision["allowed"]:
            exit_status = "stopped_no_score_gain"
            break

    if exit_status is None:
        exit_status = "stopped_max_attempts"

    audit["status"] = exit_status
    audit["exit_reason"] = exit_status
    audit["accepted_rewrites"] = accepted_rewrites
    audit["post_loop_scores"] = quality_score_payload(incumbent_scores)
    audit["post_loop_needs_revision"] = current_critique.needs_revision
    audit["post_loop_revision_items"] = _revision_items_payload(
        current_critique.revision_items,
    )
    audit["final_scores"] = quality_score_payload(incumbent_scores)
    audit["final_score_clipped"] = bool(
        getattr(incumbent_scores, "score_clipped", False),
    )
    audit["consecutive_no_score_gain"] = consecutive_no_score_gain
    audit["critic_error_count"] = len(critic_errors_payload)
    audit["approved_targets_remaining"] = current_target_state["remaining_approved_targets"]
    audit["approved_targets_cleared"] = current_target_state["cleared_approved_targets"]
    audit["approved_blocker_high_remaining_count"] = int(
        current_target_state["remaining_blocker_high_count"],
    )
    audit["approved_blocker_high_cleared_count"] = int(
        current_target_state["cleared_blocker_high_count"],
    )
    audit["approved_blocker_high_all_cleared"] = (
        int(current_target_state["remaining_blocker_high_count"]) <= 0
    )
    _write_json(polish_dir / "polish_loop.json", audit)

    if accepted_rewrites <= 0:
        if exit_status not in {"target_score_reached", "approved_targets_cleared"}:
            audit["status"] = "ran_no_improvement_found"
            _write_json(polish_dir / "polish_loop.json", audit)
        append_event(
            session,
            run,
            "controlled_polish_loop_exit",
            {
                "phase": "final_rewrite",
                "draft_version": draft_version,
                "rewrite_version": rewrite_version,
                "status": audit["status"],
                "exit_reason": exit_status,
                "accepted_rewrites": 0,
            },
        )
        # Round 0 succeeded but no structured round accepted any candidate —
        # ``incumbent`` already holds the round-0 manuscript. Return it as the
        # rewritten payload and persist ``paper_polished.md`` so the
        # downstream compliance review sees the holistic-revised text instead
        # of the pre-loop ``rewritten``.
        if round0_applied:
            round0_only_payload: dict[str, Any] = {
                "manuscript": str(incumbent["manuscript"]),
                "claim_map": list(incumbent["claim_map"]),
            }
            audit["round0_only_applied"] = True
            # PR-368 P1-3 (codex AGREE-WITH-AMENDMENTS): round-0 stage B/C
            # can change citation multiset / claim_map vs the pre-loop
            # ``rewritten``. Run a deterministic compliance check; on fail
            # fall back to ``rewritten`` (which was already validated by
            # the final-rewrite agent) so non-compliant manuscripts can't
            # silently flow into export. Audit keeps both the rejected
            # candidate's compliance and the final-output compliance.
            rejected_compliance = _validate_polish_candidate_compliance(
                candidate=round0_only_payload,
                incumbent=rewritten,
                root_original=original,
                settings=settings,
                run_dir=run_dir,
                project=project,
                session=session,
                baseline_md=baseline_md,
                policies=policies,
            )
            if rejected_compliance.failed:
                audit["compliance_revalidation"] = {
                    "outcome": "fallback_to_pre_polish",
                    "rejected_candidate_compliance": {
                        "failed": True,
                        "reason": rejected_compliance.reason,
                        "details": rejected_compliance.details,
                    },
                }
                _write_text(
                    polish_dir / "paper_polished.md",
                    str(rewritten.get("manuscript") or ""),
                )
                _write_json(polish_dir / "polish_loop.json", audit)
                # The fallback manuscript is the pre-polish ``rewritten``
                # which was already validated upstream; the caller's
                # final-output compliance is pass_.
                return rewritten, ComplianceResult.pass_(), current_summary, audit
            audit["compliance_revalidation"] = {"outcome": "passed"}
            _write_text(
                polish_dir / "paper_polished.md",
                str(incumbent["manuscript"]),
            )
            _write_json(polish_dir / "polish_loop.json", audit)
            return (
                round0_only_payload,
                ComplianceResult.pass_(),
                current_summary,
                audit,
            )
        return rewritten, ComplianceResult.pass_(), current_summary, audit

    audit["improvement_found"] = True
    # PR-368 P1-3 (codex AGREE-WITH-AMENDMENTS): the accepted-rewrite
    # return path is deliberately kept on score-monotonic-only
    # acceptance per the long-standing controlled-polish design
    # (``test_literal_polish_accepts_score_monotonic_candidate_with_high_items``
    # pins ``_validate_controlled_polish_candidate`` NEVER fires during
    # the literal polish loop). Hard revalidation only fires on the
    # NEW risk paths introduced by PR-356/360: round-0-only return and
    # critic-loop manuscript replacement.
    final_summary = _diff_summary(
        str(original.get("manuscript") or ""),
        str(incumbent["manuscript"]),
        _claim_records(original.get("claim_map")),
        _claim_records(incumbent.get("claim_map")),
    )
    _write_text(polish_dir / "paper_polished.md", str(incumbent["manuscript"]))
    _write_json(polish_dir / "polish_loop.json", audit)
    append_event(
        session,
        run,
        "controlled_polish_loop_exit",
        {
            "phase": "final_rewrite",
            "draft_version": draft_version,
            "rewrite_version": rewrite_version,
            "status": audit["status"],
            "accepted_rewrites": accepted_rewrites,
        },
    )
    return incumbent, ComplianceResult.pass_(), final_summary, audit


def _append_controlled_polish_exit_event(
    *,
    session: Session,
    run: Run,
    draft_version: str,
    rewrite_version: str,
    status: str,
    accepted_rewrites: int = 0,
    reason: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "phase": "final_rewrite",
        "draft_version": draft_version,
        "rewrite_version": rewrite_version,
        "status": status,
        "accepted_rewrites": accepted_rewrites,
    }
    if reason:
        payload["reason"] = reason
    append_event(session, run, "controlled_polish_loop_exit", payload)


def _controlled_polish_baseline_text(run_dir: Path) -> tuple[str, str]:
    from autoessay.agents.shadow_baseline import load_shadow_baseline

    baseline = load_shadow_baseline(run_dir)
    if baseline is None:
        return "", "missing"
    baseline_md = baseline.manuscript_markdown.strip()
    if not baseline_md:
        return "", "missing"
    if "stub-mode shadow baseline" in baseline_md[:200]:
        return "", "stub"
    return baseline_md, "real"


def _controlled_polish_v2_user_prompt(
    *,
    project_title: str,
    language: str,
    manuscript: str,
) -> str:
    from autoessay.agents._critic_polish_loop import (
        CONTROLLED_POLISH_EXPERT_V2_USER_TEMPLATE,
    )

    return (
        CONTROLLED_POLISH_EXPERT_V2_USER_TEMPLATE.replace(
            "{{project_title}}",
            project_title,
        )
        .replace("{{language}}", language)
        .replace("{{manuscript}}", manuscript)
    )


def _schema_partial_fields(critique: object) -> list[str]:
    fields = getattr(critique, "schema_partial_fields", [])
    if not isinstance(fields, list):
        return []
    return [str(item) for item in fields if str(item)]


def _list_payload(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _mapping_payload(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items() if isinstance(key, str)}
    return {}


def _controlled_polish_handoff_payload(
    critique: object,
    *,
    project: Project,
    rewrite_version: str,
) -> dict[str, object]:
    """Package v2 critic research-compliance findings for later phases.

    The polish loop itself only rewrites approved revision targets. Missing
    evidence, required analyses, tables/figures/formulas, and literature gaps
    are persisted as a downstream compliance handoff instead of being invented
    by the rewrite LLM.
    """

    return {
        "schema_version": "controlled_polish_handoff.v1",
        "source": "initial_v2_polish_critic",
        "project_title": project.title,
        "language": project.language,
        "rewrite_version": rewrite_version,
        "schema_partial_fields": _schema_partial_fields(critique),
        "top_journal_readiness": str(
            getattr(critique, "top_journal_readiness", "") or "",
        ),
        "editorial_decision_if_submitted_now": str(
            getattr(critique, "editorial_decision_if_submitted_now", "") or "",
        ),
        "field_identification": _mapping_payload(
            getattr(critique, "field_identification", {}),
        ),
        "scores": quality_score_payload(getattr(critique, "scores", None)),
        "main_verdict": _mapping_payload(getattr(critique, "main_verdict", {})),
        "fatal_blockers": _list_payload(getattr(critique, "fatal_blockers", [])),
        "missing_evidence_map": _list_payload(
            getattr(critique, "missing_evidence_map", []),
        ),
        "required_analyses_or_materials": _list_payload(
            getattr(critique, "required_analyses_or_materials", []),
        ),
        "required_tables_figures_formulas": _list_payload(
            getattr(critique, "required_tables_figures_formulas", []),
        ),
        "literature_revision_plan": _list_payload(
            getattr(critique, "literature_revision_plan", []),
        ),
        "frozen_issue_registry": _mapping_payload(
            getattr(critique, "frozen_issue_registry", {}),
        ),
        "final_submission_risk": _mapping_payload(
            getattr(critique, "final_submission_risk", {}),
        ),
    }


def _controlled_polish_critique_via_harness(
    *,
    manuscript: str,
    run: Run,
    project: Project,
    session: Session,
    rewrite_version: str,
    attempt: int,
    stage: str,
) -> ExpertCritiqueOutput | None:
    from autoessay.agents._critic_polish_loop import (
        CONTROLLED_POLISH_EXPERT_PROMPT,
        ExpertCritiqueOutput,
    )

    prompt = _controlled_polish_v2_user_prompt(
        project_title=project.title,
        language=project.language,
        manuscript=manuscript,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": CONTROLLED_POLISH_EXPERT_PROMPT,
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=16000,
        response_format={"type": "json_object"},
        request_id=f"final_rewrite_controlled_polish_critique_{rewrite_version}_{stage}_{attempt}",
        prompt_template_id="final_rewrite.controlled_polish.critique.v2",
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id="final_rewrite.controlled_polish.critique",
        user_id=project.user_id,
        attempt=attempt,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "final_rewrite",
            "rewrite_version": rewrite_version,
            "polish_stage": stage,
            "critic_prompt_version": "v2_top_journal_one_shot",
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=HookRegistry(),
                context=context,
                output_schema=ExpertCritiqueOutput,
                audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="FinalRewrite"),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001 - controlled polish is optional.
        return None
    parsed = response.parsed
    return parsed if isinstance(parsed, ExpertCritiqueOutput) else None


_OPEN_PROMPT_TURN_1 = (
    "下面是一份学术论文稿件。"
    "请极其客观的评价一下这篇文章的学术水平与价值。并提出修改意见。\n\n"
    "——稿件全文（markdown）——\n\n"
)

_OPEN_PROMPT_TURN_2 = (
    "请按照你的要求，直接给出完整的修改稿，markdown 格式。"
    "只输出修订后的论文正文，不要任何解释、不要 markdown 代码块包装、不要 JSON。"
    "如果你认为某些结论需要数学公式、变量定义表、回归结果表、或描述性统计来证明，"
    "必须把这些公式（LaTeX，$$...$$ 或 $...$）、markdown 表、或数据占位（如【待填】）写入修订稿。"
    "在缺少真实数据的情况下，宁可用【待填】占位也不要编造系数、p值、显著性、样本量或回归数值。"
)


def _controlled_polish_holistic_round0_open_prompt(
    *,
    manuscript: str,
    run: Run,
    project: Project,
    session: Session,
    rewrite_version: str,
) -> str | None:
    """Round-0 stage B: open-prompt foundation-model rewrite.

    Two-turn natural conversation against gpt-5.5 via the existing one-api
    gateways (rightcode + apiport), overriding the per-provider model. The
    model receives the manuscript with no system prompt, no JSON schema,
    no citation hard constraint — it is free to do the strategic edits
    (delete unsupported empirical claims, mark待填 placeholders, add
    canonical references, restructure sections) that the V2 schema-bound
    prompts structurally prevent.

    Returns the model's revised markdown (stage B output), or ``None`` on
    transport failure / empty response. The caller is responsible for
    feeding stage B through the pipeline's V2 rewriter (stage C) to
    re-anchor it in source-pool / citation-multiset / claim-map invariants.

    Codex AGREE-WITH-AMENDMENTS 2026-05-12 v2 (after canary): the previous
    "1 LLM call with V2 system + critique JSON anchor + JSON output +
    citation multiset sanity gate" design was scrapped because the V2
    schema + citation hard constraint structurally prevent the strategic
    moves round 0 was supposed to enable.
    """
    import httpx

    from autoessay.config import LLMProviderSpec, get_llm_providers
    from autoessay.llm_client import LLMClient

    prod_providers = get_llm_providers()
    # Build a gpt-5.5 chain. Empirically (2026-05-12 canary): apiport.cc.cd
    # has an aggressive ~100s Cloudflare edge timeout that 524s on gpt-5.5
    # critique+rewrite calls (model takes 85-200s end-to-end); rightcode
    # responds in ~85s for critique and tolerates longer rewrite. Use
    # rightcode only for stage B and fall back to apiport only if rightcode
    # is missing entirely. MiniMax does not route gpt-5.5 (MiniMax-M2.7
    # only) so it is excluded.
    rightcode_providers = [
        LLMProviderSpec(
            name=f"{p.name}_gpt55",
            base_url=p.base_url,
            api_key=p.api_key,
            model="gpt-5.5",
        )
        for p in prod_providers
        if "rightcode" in p.name.lower()
    ]
    apiport_providers = [
        LLMProviderSpec(
            name=f"{p.name}_gpt55",
            base_url=p.base_url,
            api_key=p.api_key,
            model="gpt-5.5",
        )
        for p in prod_providers
        if "apiport" in p.name.lower()
    ]
    gpt55_providers = rightcode_providers or apiport_providers
    if not gpt55_providers:
        return None
    # Per-request httpx client with a 900s ceiling — gpt-5.5 streaming
    # rewrite can spend 4-6 minutes producing the full revised
    # manuscript. The default ``settings.llm_request_timeout_seconds``
    # (180s) is for non-streaming calls; streaming connections need to
    # stay open for the duration of model reasoning. 900s gives a
    # comfortable ceiling without unbounding the call.
    # PR-369 P2-4 (codex review): the timeout MUST be passed to
    # ``LLMClient`` explicitly via ``timeout_seconds``. Setting it on
    # the injected ``httpx.AsyncClient`` alone was silently overridden
    # by ``self._timeout_seconds`` (which read the 180s default from
    # settings) at every ``stream(timeout=...)`` call site.
    http_client = httpx.AsyncClient(timeout=900.0)
    client = LLMClient(
        providers=gpt55_providers,
        http_client=http_client,
        timeout_seconds=900.0,
    )

    turn_1_user = _OPEN_PROMPT_TURN_1 + manuscript

    # PR-369 X-3 (codex review): emit progress events at each Stage B
    # turn boundary so a stalled call surfaces in the timeline before
    # the RQ 90-min ceiling hits. Caller commits per event.
    def _emit_progress(stage: str, status: str, extra: dict[str, object] | None = None) -> None:
        with contextlib.suppress(Exception):
            payload: dict[str, object] = {
                "phase": "final_rewrite",
                "subphase": "round0_stage_b",
                "stage": stage,
                "status": status,
                "rewrite_version": rewrite_version,
            }
            if extra:
                payload.update(extra)
            append_event(session, run, "phase_progress", payload)
            session.commit()

    _emit_progress("turn1_critique", "started", {"manuscript_chars": len(manuscript)})

    async def _two_turn_call() -> str | None:
        # 2026-05-12 D+: both calls now use stream=True so SSE chunks
        # start within 1-5s of the request, well under the gateway
        # Cloudflare edge timeout (100-125s). gpt-5.5 reasoning for
        # the rewrite can take 4-6 minutes; streaming keeps the
        # connection alive throughout. Previous max_tokens caps (8000
        # critique / 12000 rewrite) are restored to the natural
        # ranges (8000 / 32000) — streaming removes the
        # Cloudflare-driven need to truncate.
        try:
            critique_response = await client.chat_completion(
                [{"role": "user", "content": turn_1_user}],
                "gpt-5.5",
                0.7,
                8000,
                retries=0,
                response_format=None,
                validate_json_content=False,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001 - round 0 must never abort the loop.
            _emit_progress(
                "turn1_critique",
                "failed_transport",
                {"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            await http_client.aclose()
            return None
        critique_text = str(critique_response.get("content", "")).strip()
        if not critique_text:
            _emit_progress("turn1_critique", "failed_empty")
            await http_client.aclose()
            return None
        _emit_progress(
            "turn1_critique",
            "succeeded",
            {"critique_chars": len(critique_text)},
        )
        _emit_progress("turn2_rewrite", "started")
        try:
            rewrite_response = await client.chat_completion(
                [
                    {"role": "user", "content": turn_1_user},
                    {"role": "assistant", "content": critique_text},
                    {"role": "user", "content": _OPEN_PROMPT_TURN_2},
                ],
                "gpt-5.5",
                0.7,
                32000,
                retries=0,
                response_format=None,
                validate_json_content=False,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            _emit_progress(
                "turn2_rewrite",
                "failed_transport",
                {"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            await http_client.aclose()
            return None
        await http_client.aclose()
        revised = str(rewrite_response.get("content", "")).strip()
        if not revised:
            _emit_progress("turn2_rewrite", "failed_empty")
        else:
            _emit_progress(
                "turn2_rewrite",
                "succeeded",
                {"revised_chars": len(revised)},
            )
        # Some gateways still wrap the response in a markdown code fence
        # even when asked not to; strip a single outer ```markdown ... ```
        # if present.
        if revised.startswith("```"):
            lines = revised.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            revised = "\n".join(lines).strip()
        return revised or None

    try:
        result = asyncio.run(_two_turn_call())
    except Exception:  # noqa: BLE001 - belt-and-braces; never let round 0 raise.
        with contextlib.suppress(Exception):
            asyncio.run(http_client.aclose())
        return None
    return result


def _controlled_polish_rewrite_via_harness(
    *,
    manuscript: str,
    critique: object,
    target_items: Sequence[object],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    rewrite_version: str,
    attempt: int,
    policies: EvidencePolicies,
) -> str | None:
    from autoessay.agents._critic_polish_loop import quality_scores_dict

    incumbent_paragraphs = _paragraphs(manuscript)
    incumbent_citation_sequence = drafter._extract_inline_citations(manuscript)
    critique_scores = quality_scores_dict(getattr(critique, "scores", None))
    payload = {
        "project_title": project.title,
        "language": project.language,
        "driver_dimensions": ["compliance", "completeness"],
        "novelty_policy": "score-only guard; do not add new material for novelty",
        "scores": critique_scores,
        "target_revision_items": _revision_items_payload(target_items),
        "deletion_or_compression_plan": _list_payload(
            getattr(critique, "deletion_or_compression_plan", []),
        ),
        "acceptance_contract": {
            "revision_items_are_audit_only": True,
            "rewrite_scope_locked_to_approved_targets": True,
            "new_critic_items_do_not_expand_scope": True,
            "scores_must_be_monotonic_vs_incumbent": True,
            "dimensions": ["compliance", "novelty", "completeness"],
            "exit_when_approved_blocker_high_targets_cleared": True,
            "exit_when_all_scores_at_least": 9.5,
            "exit_after_consecutive_no_score_gain_attempts": 2,
            "downstream_compliance_review": (
                "critic, integrity, and exports phases will review compliance after this loop"
            ),
        },
        "incumbent_paragraph_count": len(incumbent_paragraphs),
        "incumbent_citation_sequence": incumbent_citation_sequence,
        "incumbent_paragraph_marker_sequences": [
            drafter._extract_inline_citations(paragraph) for paragraph in incumbent_paragraphs
        ],
        "manuscript": manuscript,
    }
    prompt = (
        "专家修改执行 prompt：请根据 target_revision_items 做一轮字面版 polish。"
        "target_revision_items 来自 iter 0 一次性专家评审固化的 approved_targets；"
        "只改 target_revision_items 涉及的必要 scope，输出完整 markdown。"
        "target_revision_items 中的 original_text_anchor、expected_output_after_revision、"
        "acceptance_test、later_review_rule 是验收依据，必须优先满足。"
        "如果 deletion_or_compression_plan 中的 DELETE/COMPRESS/MERGE/MOVE 动作与"
        "target_revision_items 的 scope 对齐，可以执行对应删减、压缩、合并或移动；"
        "不要处理与 target_revision_items 无关的删除压缩建议。"
        "不要处理后续 critic 新冒出来、未列入 approved_targets 的修改意见。"
        "下一轮专家 critic 会给候选稿打分；revision_items 只进入 audit，不参与接受判定。"
        "候选稿只要 compliance / novelty / completeness 三维分数都不低于 incumbent 就会被接受。"
        "loop 会在 approved_targets 中的 BLOCKER/HIGH 全部 cleared、三维分数都达到 9.5、"
        "连续两轮没有任何维度增长，或达到 5 次上限时退出。"
        "保持已有 citation 和事实边界；不得主动新增 source_id、作者名、出版年份、书名、"
        "统计数字、档案名或因果断言。"
        "如果某个修改会越过证据边界，请改成更保守的表述或保持原句。"
        '只返回 JSON：{"manuscript": "..."}。\n\n'
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )
    settings = get_settings()
    baseline_as_evidence_directive = _baseline_as_evidence_test_rewrite_directive(Path(run.run_dir))
    material_scope_directive = drafter._material_scope_guard_directive(
        drafter._load_material_diagnostic(Path(run.run_dir)),
        selected_thesis=_selected_thesis_for_material_scope(Path(run.run_dir)),
        research_kernel=getattr(run, "research_kernel_json", None),
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    CONTROLLED_POLISH_REWRITE_SYSTEM_PROMPT
                    + "\n\n"
                    + policies.section_directive_prefix()
                    + (
                        ("\n\n" + policies.whitelist_directive)
                        if policies.whitelist_directive
                        else ""
                    )
                    + " "
                    + language_directive(project.language)
                    + baseline_as_evidence_directive
                    + material_scope_directive
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=settings.one_api_model,
        temperature=0.1,
        max_tokens=25000,
        response_format={"type": "json_object"},
        request_id=f"final_rewrite_controlled_polish_rewrite_{rewrite_version}_a{attempt}",
        prompt_template_id="final_rewrite.controlled_polish.rewrite.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id="final_rewrite.controlled_polish.rewrite",
        user_id=project.user_id,
        attempt=attempt,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "final_rewrite",
            "rewrite_version": rewrite_version,
            "target_revision_count": len(target_items),
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=ControlledPolishRewriteOutput,
                audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="FinalRewrite"),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001 - rejected candidate keeps incumbent.
        return None
    parsed = response.parsed
    if isinstance(parsed, ControlledPolishRewriteOutput):
        return parsed.manuscript.strip()
    if isinstance(parsed, Mapping):
        try:
            output = ControlledPolishRewriteOutput.parse_obj(parsed)
        except ValidationError:
            return None
        return output.manuscript.strip()
    return None


def _validate_controlled_polish_candidate(
    *,
    candidate: Mapping[str, object],
    incumbent: Mapping[str, object],
    root_original: Mapping[str, object],
    settings: Settings,
    run_dir: Path,
    project: Project,
    session: Session,
    baseline_md: str,
    policies: EvidencePolicies,
) -> ControlledPolishValidation:
    from autoessay.agents._critic_polish_loop import (
        compute_anti_plagiarism_jaccard,
        is_anti_plagiarism_violation,
    )

    candidate_text = str(candidate.get("manuscript") or "")
    root_original_text = str(root_original.get("manuscript") or "")
    claim_map = _claim_records(candidate.get("claim_map"))
    reasons: list[str] = []
    details: list[dict[str, object]] = []

    if not candidate_text.strip():
        reasons.append("empty_candidate")
    expected_citations = drafter._extract_inline_citations(root_original_text)
    actual_citations = drafter._extract_inline_citations(candidate_text)
    if Counter(actual_citations) != Counter(expected_citations):
        reasons.append("citation_multiset_mismatch")
        details.append(
            {
                "basis": "root_original",
                "expected": expected_citations,
                "actual": actual_citations,
            }
        )
    if len(candidate_text) < len(root_original_text) * 0.95:
        reasons.append("length_below_95pct_root_original")
        details.append(
            {
                "basis": "root_original",
                "before": len(root_original_text),
                "after": len(candidate_text),
            }
        )

    incumbent_text = str(incumbent.get("manuscript") or "")
    before_paragraphs = _paragraphs(incumbent_text)
    after_paragraphs = _paragraphs(candidate_text)
    if len(before_paragraphs) != len(after_paragraphs):
        reasons.append("paragraph_count_changed")
        details.append(
            {
                "basis": "incumbent",
                "before": len(before_paragraphs),
                "after": len(after_paragraphs),
            }
        )
    else:
        changed_citation_paragraphs: list[dict[str, object]] = []
        for index, (before_para, after_para) in enumerate(
            zip(before_paragraphs, after_paragraphs, strict=True),
        ):
            before_markers = drafter._extract_inline_citations(before_para)
            if not before_markers:
                continue
            after_markers = drafter._extract_inline_citations(after_para)
            if before_markers != after_markers:
                changed_citation_paragraphs.append(
                    {
                        "basis": "incumbent",
                        "index": index,
                        "before": before_markers,
                        "after": after_markers,
                    }
                )
        if changed_citation_paragraphs:
            reasons.append("citation_bearing_paragraph_marker_sequence_changed")
            details.extend(changed_citation_paragraphs[:5])

    sentinel_matches = re.findall(
        r"\[UNCITED\]|TODO(?:_EVIDENCE)?|FIXME|<\s*TODO\b",
        candidate_text,
        flags=re.IGNORECASE,
    )
    if sentinel_matches:
        reasons.append("unresolved_marker_or_todo")
        details.append({"matches": sentinel_matches[:10]})

    cnki_errors = _controlled_polish_cnki_structure_errors(candidate_text, project.language)
    if cnki_errors:
        reasons.append("cnki_structure_incomplete")
        details.append({"cnki_errors": cnki_errors})

    whitelist = _shortlist_source_ids(run_dir)
    if whitelist:
        new_source_ids = sorted(
            {
                source_id
                for claim in claim_map
                for source_id in _source_ids(claim.get("source_ids"))
                if source_id not in whitelist
            }
        )
        if new_source_ids:
            reasons.append("source_id_not_in_whitelist")
            details.append({"source_ids": new_source_ids})

    marker_errors = _cite_marker_resolution_errors(candidate_text, run_dir)
    if marker_errors:
        reasons.append("cite_marker_gate_failed")
        details.append({"errors": marker_errors})

    compliance_against_original = _run_post_rewrite_compliance(
        rewritten={"manuscript": candidate_text, "claim_map": claim_map},
        original=dict(root_original),
        settings=settings,
        run_dir=run_dir,
        policies=policies,
        run=None,
        session=None,
        project=project,
    )
    if compliance_against_original.failed:
        reasons.append(f"original_compliance_failed:{compliance_against_original.reason}")
        details.extend(compliance_against_original.details or [])

    if baseline_md:
        jaccard = compute_anti_plagiarism_jaccard(
            candidate_text,
            baseline_md,
            project.language,
        )
        details.append({"anti_plagiarism_jaccard": jaccard})
        if is_anti_plagiarism_violation(candidate_text, baseline_md, project.language):
            reasons.append("anti_plagiarism_jaccard_violation")

    return ControlledPolishValidation(
        passed=not reasons,
        reasons=reasons,
        details=details,
    )


def _controlled_polish_cnki_structure_errors(text: str, language: str) -> list[str]:
    cnki_heading_pattern = r"(?m)^\s*(?:#{1,6}\s*)?[一二三四五六七八]、"
    if language not in {"zh", "ja"} and not re.search(cnki_heading_pattern, text):
        return []
    errors: list[str] = []
    for marker in ("摘要", "关键词", "参考文献"):
        if not re.search(rf"(?m)^\s*(?:#{{1,6}}\s*)?{marker}(?:[：:]|\s*$)", text):
            errors.append(f"missing_{marker}")
    found = re.findall(r"(?m)^\s*(?:#{1,6}\s*)?([一二三四五六七八]、)", text)
    expected = ["一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、"]
    if found[:8] != expected:
        errors.append("body_section_order_incomplete")
    return errors


def _high_blocker_revision_items(critique: object) -> list[object]:
    items = getattr(critique, "revision_items", [])
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if _revision_item_value(item, "severity").upper() in {"BLOCKER", "HIGH"}
        and _revision_item_value(item, "scope")
        and _revision_item_value(item, "scope") not in {"全文", "整文", "whole paper"}
    ]


def _revision_item_value(item: object, key: str) -> str:
    if isinstance(item, Mapping):
        return str(item.get(key) or "").strip()
    return str(getattr(item, key, "") or "").strip()


def _normalize_revision_item_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _revision_item_fingerprint(item: object) -> str:
    severity = _normalize_revision_item_value(_revision_item_value(item, "severity")).upper()
    scope = _normalize_revision_item_value(_revision_item_value(item, "scope"))
    issue = _normalize_revision_item_value(_revision_item_value(item, "issue"))
    if severity not in {"BLOCKER", "HIGH", "MEDIUM", "LOW"} or not scope or not issue:
        return ""
    raw = "|".join([severity, scope, issue])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _revision_items_with_fingerprints(items: Sequence[object]) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in items:
        fingerprint = _revision_item_fingerprint(item)
        if not fingerprint:
            continue
        payloads = _revision_items_payload([item])
        payload = dict(payloads[0]) if payloads else {}
        payload["fingerprint"] = fingerprint
        payload["severity"] = _normalize_revision_item_value(
            _revision_item_value(item, "severity"),
        ).upper()
        payload["scope"] = _normalize_revision_item_value(_revision_item_value(item, "scope"))
        payload["issue"] = _normalize_revision_item_value(_revision_item_value(item, "issue"))
        output.append(payload)
    return output


def _approved_targets_from_revision_items(items: Sequence[object]) -> list[dict[str, object]]:
    approved_targets: list[dict[str, object]] = []
    seen: set[str] = set()
    for payload in _revision_items_with_fingerprints(items):
        fingerprint = str(payload.get("fingerprint") or "")
        if not fingerprint or fingerprint in seen:
            continue
        seen.add(fingerprint)
        approved_targets.append(payload)
    return approved_targets


def _approved_target_state(
    approved_targets: Sequence[Mapping[str, object]],
    revision_items: Sequence[object],
) -> ApprovedTargetState:
    current_items = _revision_items_with_fingerprints(revision_items)
    current_fingerprints = {str(item.get("fingerprint") or "") for item in current_items}
    approved_fingerprints = {
        str(target.get("fingerprint") or "")
        for target in approved_targets
        if str(target.get("fingerprint") or "")
    }
    remaining_approved = [
        dict(target)
        for target in approved_targets
        if str(target.get("fingerprint") or "") in current_fingerprints
    ]
    cleared_approved = [
        dict(target)
        for target in approved_targets
        if str(target.get("fingerprint") or "") not in current_fingerprints
    ]
    approved_blocker_high = [
        dict(target)
        for target in approved_targets
        if str(target.get("severity") or "").upper() in {"BLOCKER", "HIGH"}
    ]
    remaining_blocker_high = [
        target
        for target in remaining_approved
        if str(target.get("severity") or "").upper() in {"BLOCKER", "HIGH"}
    ]
    cleared_blocker_high = [
        target
        for target in cleared_approved
        if str(target.get("severity") or "").upper() in {"BLOCKER", "HIGH"}
    ]
    critic_errors = [
        item
        for item in current_items
        if str(item.get("severity") or "").upper() in {"BLOCKER", "HIGH"}
        and str(item.get("fingerprint") or "") not in approved_fingerprints
    ]
    return {
        "approved_blocker_high_count": len(approved_blocker_high),
        "remaining_approved_count": len(remaining_approved),
        "remaining_approved_targets": remaining_approved,
        "cleared_approved_count": len(cleared_approved),
        "cleared_approved_targets": cleared_approved,
        "remaining_blocker_high_count": len(remaining_blocker_high),
        "remaining_blocker_high_targets": remaining_blocker_high,
        "cleared_blocker_high_count": len(cleared_blocker_high),
        "cleared_blocker_high_targets": cleared_blocker_high,
        "critic_error_count": len(critic_errors),
        "critic_errors": critic_errors,
    }


def _revision_issue_signature(items: Sequence[object]) -> str:
    if not items:
        return "none"
    pieces = [
        "|".join(
            [
                str(getattr(item, "severity", "")),
                str(getattr(item, "scope", "")),
                str(getattr(item, "issue", ""))[:120],
            ]
        )
        for item in items
    ]
    return hashlib.sha256("\n".join(sorted(pieces)).encode("utf-8")).hexdigest()[:16]


def _revision_items_payload(items: Sequence[object]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for item in items:
        if hasattr(item, "dict"):
            raw = item.dict()
            if isinstance(raw, dict):
                payload.append({key: value for key, value in raw.items() if isinstance(key, str)})
                continue
        if isinstance(item, Mapping):
            payload.append({key: value for key, value in item.items() if isinstance(key, str)})
    return payload


def quality_score_payload(scores: object) -> dict[str, object] | None:
    if scores is None:
        return None
    if hasattr(scores, "dict"):
        raw = scores.dict()
        if isinstance(raw, dict):
            return {key: value for key, value in raw.items() if isinstance(key, str)}
    if isinstance(scores, Mapping):
        return {key: value for key, value in scores.items() if isinstance(key, str)}
    return None


def _scores_monotonic(candidate: object, incumbent: object) -> bool:
    for dim in ("compliance", "novelty", "completeness"):
        candidate_value = getattr(candidate, dim, None)
        incumbent_value = getattr(incumbent, dim, None)
        if candidate_value is None or incumbent_value is None:
            return False
        if float(candidate_value) < float(incumbent_value):
            return False
    return True


def _scores_any_gain(candidate: object, incumbent: object) -> bool:
    for dim in ("compliance", "novelty", "completeness"):
        candidate_value = getattr(candidate, dim, None)
        incumbent_value = getattr(incumbent, dim, None)
        if candidate_value is None or incumbent_value is None:
            return False
        if float(candidate_value) > float(incumbent_value):
            return True
    return False


def _scores_all_at_least(scores: object, threshold: float) -> bool:
    for dim in ("compliance", "novelty", "completeness"):
        value = getattr(scores, dim, None)
        if value is None or float(value) < threshold:
            return False
    return True


def _approved_targets_exit_decision(target_state: ApprovedTargetState) -> dict[str, object]:
    remaining = int(target_state["remaining_blocker_high_count"])
    return {
        "condition": "approved_targets_cleared",
        "approved_blocker_high_remaining_count": remaining,
        "allowed": remaining <= 0,
    }


def _target_score_exit_decision(
    scores: object,
    target_state: ApprovedTargetState,
    threshold: float,
) -> dict[str, object]:
    remaining = int(target_state["remaining_blocker_high_count"])
    score_clipped = bool(getattr(scores, "score_clipped", False))
    scores_all_at_least = _scores_all_at_least(scores, threshold)
    return {
        "condition": "target_score_reached",
        "threshold": threshold,
        "scores_all_at_least": scores_all_at_least,
        "score_clipped": score_clipped,
        "approved_blocker_high_remaining_count": remaining,
        "allowed": scores_all_at_least and remaining <= 0 and not score_clipped,
    }


def _no_score_gain_exit_decision(consecutive_no_score_gain: int) -> dict[str, object]:
    return {
        "condition": "no_score_gain",
        "consecutive_no_score_gain": consecutive_no_score_gain,
        "no_score_gain_patience": CONTROLLED_POLISH_NO_SCORE_GAIN_PATIENCE,
        "allowed": consecutive_no_score_gain >= CONTROLLED_POLISH_NO_SCORE_GAIN_PATIENCE,
    }


def _scores_strictly_better(candidate: object, incumbent: object) -> bool:
    if not _scores_monotonic(candidate, incumbent):
        return False
    for dim in ("compliance", "completeness"):
        candidate_value = getattr(candidate, dim, None)
        incumbent_value = getattr(incumbent, dim, None)
        if candidate_value is None or incumbent_value is None:
            return False
        if float(candidate_value) > float(incumbent_value):
            return True
    return False


def _validation_payload(validation: ControlledPolishValidation) -> dict[str, object]:
    return {
        "passed": validation.passed,
        "reasons": list(validation.reasons),
        "details": list(validation.details),
    }


def _selected_thesis_for_material_scope(run_dir: Path) -> dict[str, object]:
    selected = drafter._load_json_mapping(run_dir / "novelty" / "selected_thesis.json")
    return selected if selected else {}


def _maybe_apply_material_scope_calibration(
    payload: Mapping[str, object],
    *,
    run_dir: Path,
    run: Run,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Downgrade over-definitive claims when material diagnostics are insufficient.

    This is a deterministic backstop for the same production material-scope
    guard used by drafter prompts. It preserves citation markers and claim_map
    structure; it only changes overclaiming wording before final compliance.
    """
    summary = drafter._material_scope_guard_summary(
        drafter._load_material_diagnostic(run_dir),
        selected_thesis=_selected_thesis_for_material_scope(run_dir),
        research_kernel=getattr(run, "research_kernel_json", None),
    )
    audit: dict[str, object] = {
        "applied": bool(summary.get("applied")),
        "changed": False,
    }
    copied: dict[str, Any] = dict(payload)
    if not summary.get("applied"):
        return copied, audit

    before_manuscript = str(copied.get("manuscript") or "")
    after_manuscript = drafter._rewrite_material_scope_text(before_manuscript)
    raw_claim_map = copied.get("claim_map")
    after_claim_map: list[dict[str, object]] = []
    claim_changed = False
    if isinstance(raw_claim_map, list):
        for raw_claim in raw_claim_map:
            if not isinstance(raw_claim, Mapping):
                continue
            claim = dict(raw_claim)
            raw_text = claim.get("claim_text")
            if isinstance(raw_text, str):
                rewritten_text = drafter._rewrite_material_scope_text(raw_text)
                if rewritten_text != raw_text:
                    claim["claim_text"] = rewritten_text
                    claim_changed = True
            after_claim_map.append(claim)
        copied["claim_map"] = after_claim_map
    if after_manuscript != before_manuscript:
        copied["manuscript"] = after_manuscript
    changed = after_manuscript != before_manuscript or claim_changed
    audit.update(
        {
            "changed": changed,
            "manuscript_changed": after_manuscript != before_manuscript,
            "claim_map_changed": claim_changed,
            "recommended_action": summary.get("recommended_action"),
            "missing_materials": summary.get("missing_materials", []),
        }
    )
    return copied, audit


def _citation_multiset_retry_guidance(manuscript: object) -> str:
    marker_multiset = sorted(drafter._extract_inline_citations(str(manuscript or "")))
    return (
        "citation_preservation_retry: 上一次 final rewrite 因 cite_marker_multiset_change "
        "被拒绝。请重新改写，但必须逐字保留原稿全部 citation marker 的编号与出现次数；"
        "不要新增、删除、合并、重排、替换任何 [N]。可改写 marker 之间的文字、调整段落和"
        "压缩重复，但每个原有 [N] 必须仍出现在改写稿中。claim_map 必须保持与输入相同的"
        "条目数、paragraph_id 集合和顺序；不要合并 claim，不要删除 citation-bearing "
        "paragraph。原稿 citation marker multiset: "
        f"{json.dumps(marker_multiset, ensure_ascii=False)}"
    )


def _maybe_pre_repair_numeric_citations(
    payload: Mapping[str, object],
    *,
    run_dir: Path | None,
) -> tuple[dict[str, Any], dict[str, object]]:
    """Repair stale numeric markers before final-rewrite compliance.

    The drafter/exporter citation model numbers references from the final
    cited-source subset, while intermediate LLM stages may preserve shortlist
    ordinal markers such as ``[15]``. Export already performs this repair, but
    final-rewrite compliance runs earlier; without the same deterministic
    repair, a mechanically fixable stale marker can discard an otherwise useful
    global rewrite. Only unresolved numeric markers trigger this path, so normal
    citation multiset changes are still caught by the compliance gate.
    """
    copied: dict[str, Any] = dict(payload)
    text = str(copied.get("manuscript") or "")
    text, markdown_newlines_normalized = _normalize_escaped_markdown_newlines(text)
    if markdown_newlines_normalized:
        copied["manuscript"] = text
    before_errors = _cite_marker_resolution_errors(text, run_dir)
    unresolved_errors = [error for error in before_errors if "does not resolve" in error]
    audit: dict[str, object] = {
        "changed": markdown_newlines_normalized,
        "markdown_newlines_normalized": markdown_newlines_normalized,
        "unresolved_before": unresolved_errors,
        "unresolved_after": unresolved_errors,
    }
    if run_dir is None:
        return copied, audit

    cited_sources = _cited_sources_for_rewrite(run_dir)
    if not cited_sources:
        return copied, audit

    repaired = _repair_raw_source_id_markers_for_rewrite(text, cited_sources=cited_sources)
    if repaired != text:
        copied["manuscript"] = repaired
        text = repaired
        audit["changed"] = True

    if not unresolved_errors:
        after_errors = _cite_marker_resolution_errors(text, run_dir)
        audit["unresolved_after"] = [error for error in after_errors if "does not resolve" in error]
        return copied, audit

    claim_map = _claim_records(copied.get("claim_map"))
    if not claim_map:
        return copied, audit

    repaired = _repair_numeric_citations_from_claim_map_for_rewrite(
        text,
        claim_map=claim_map,
        cited_sources=cited_sources,
    )
    after_errors = _cite_marker_resolution_errors(repaired, run_dir)
    after_unresolved = [error for error in after_errors if "does not resolve" in error]
    audit["unresolved_after"] = after_unresolved
    if repaired != text:
        copied["manuscript"] = repaired
        audit["changed"] = True
    return copied, audit


def _normalize_escaped_markdown_newlines(manuscript: str) -> tuple[str, bool]:
    if "\\n" not in manuscript and "\\r" not in manuscript:
        return manuscript, False
    escaped_linebreaks = len(re.findall(r"\\r\\n|\\n|\\r", manuscript))
    if escaped_linebreaks < 2:
        return manuscript, False

    real_linebreaks = manuscript.count("\n") + manuscript.count("\r")
    escaped_paragraphs = manuscript.count("\\n\\n") + manuscript.count("\\r\\n\\r\\n")
    real_paragraphs = manuscript.count("\n\n") + manuscript.count("\r\n\r\n")
    if escaped_paragraphs == 0 and real_linebreaks > 1:
        return manuscript, False
    if real_linebreaks > max(2, escaped_linebreaks // 2) and real_paragraphs >= escaped_paragraphs:
        return manuscript, False

    normalized = re.sub(r"\\r\\n|\\r|\\n", "\n", manuscript)
    return normalized, normalized != manuscript


def _repair_raw_source_id_markers_for_rewrite(
    manuscript: str,
    *,
    cited_sources: Sequence[Any],
) -> str:
    if not manuscript.strip() or not cited_sources:
        return manuscript
    source_to_tag = {
        str(getattr(source, "source_id", "")): f"[{index}]"
        for index, source in enumerate(cited_sources, 1)
        if str(getattr(source, "source_id", ""))
    }
    if not source_to_tag:
        return manuscript
    return "\n\n".join(
        _replace_raw_source_id_markers_for_rewrite(paragraph, source_to_tag)
        for paragraph in manuscript.split("\n\n")
    )


def _cited_sources_for_rewrite(run_dir: Path) -> list[Any]:
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        return []
    metadata = _load_json_mapping(draft_dir / "draft_metadata.json")
    cited_raw = metadata.get("cited_sources")
    if not isinstance(cited_raw, list):
        return []
    cited_ids = [item for item in cited_raw if isinstance(item, str) and item]
    if not cited_ids:
        return []
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    by_id = {
        str(getattr(source, "source_id", "")): source
        for source in shortlist
        if str(getattr(source, "source_id", ""))
    }
    return [by_id[source_id] for source_id in cited_ids if source_id in by_id]


def _repair_numeric_citations_from_claim_map_for_rewrite(
    manuscript: str,
    *,
    claim_map: Sequence[Mapping[str, object]],
    cited_sources: Sequence[Any],
) -> str:
    if not manuscript.strip() or not cited_sources:
        return manuscript
    source_to_tag = {
        str(getattr(source, "source_id", "")): f"[{index}]"
        for index, source in enumerate(cited_sources, 1)
        if str(getattr(source, "source_id", ""))
    }
    paragraph_tags = _claim_map_paragraph_tags_for_rewrite(claim_map, source_to_tag)
    if not paragraph_tags:
        return manuscript

    paragraphs = manuscript.split("\n\n")
    repaired: list[str] = []
    claim_index = 0
    max_ref = len(cited_sources)
    current_heading = ""
    in_references = False
    for paragraph in paragraphs:
        stripped = paragraph.strip()
        if not stripped:
            repaired.append(paragraph)
            continue
        if _is_markdown_heading(stripped):
            current_heading = stripped
            if re.search(r"参考文献|references\b", stripped, flags=re.IGNORECASE):
                in_references = True
            repaired.append(paragraph)
            continue
        if in_references or _is_front_matter_paragraph(stripped, current_heading):
            repaired.append(paragraph)
            continue

        expected_tags = paragraph_tags[claim_index] if claim_index < len(paragraph_tags) else []
        claim_index += 1
        paragraph = _replace_raw_source_id_markers_for_rewrite(paragraph, source_to_tag)
        current_nums = [int(value) for value in re.findall(r"\[(\d{1,3})\]", paragraph)]
        if expected_tags:
            if current_nums:
                repaired.append(
                    _replace_numeric_citation_clusters_for_rewrite(paragraph, expected_tags)
                )
            else:
                repaired.append(_append_tags_to_paragraph_for_rewrite(paragraph, expected_tags))
        elif any(number > max_ref for number in current_nums):
            repaired.append(re.sub(r"(?:\s*\[\d{1,3}\])+", "", paragraph).rstrip())
        else:
            repaired.append(paragraph)
    return "\n\n".join(repaired).rstrip() + "\n"


def _claim_map_paragraph_tags_for_rewrite(
    claim_map: Sequence[Mapping[str, object]],
    source_to_tag: Mapping[str, str],
) -> list[list[str]]:
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for index, claim in enumerate(claim_map, 1):
        paragraph_id = str(claim.get("paragraph_id") or f"claim-{index:03d}")
        if paragraph_id not in grouped:
            grouped[paragraph_id] = []
            order.append(paragraph_id)
        for source_id in _source_ids(claim.get("source_ids")):
            tag = source_to_tag.get(source_id)
            if tag and tag not in grouped[paragraph_id]:
                grouped[paragraph_id].append(tag)
    return [grouped[paragraph_id] for paragraph_id in order]


def _replace_raw_source_id_markers_for_rewrite(
    paragraph: str,
    source_to_tag: Mapping[str, str],
) -> str:
    repaired = paragraph
    ordered_sources = sorted(source_to_tag.items(), key=lambda item: len(item[0]), reverse=True)
    for source_id, tag in ordered_sources:
        escaped = re.escape(source_id)
        repaired = re.sub(rf"[\[［【]\s*{escaped}\s*[\]］】]", tag, repaired)
        repaired = re.sub(rf"[（(]\s*{escaped}\s*[）)]", tag, repaired)
    return repaired


def _replace_numeric_citation_clusters_for_rewrite(
    paragraph: str,
    expected_tags: Sequence[str],
) -> str:
    canonical = "".join(expected_tags)
    replaced = False

    def repl(_match: re.Match[str]) -> str:
        nonlocal replaced
        if not replaced:
            replaced = True
            return canonical
        return ""

    return re.sub(r"(?:\[\d{1,3}\]\s*)+", repl, paragraph)


def _append_tags_to_paragraph_for_rewrite(paragraph: str, expected_tags: Sequence[str]) -> str:
    canonical = "".join(expected_tags)
    stripped = paragraph.rstrip()
    if not stripped:
        return paragraph
    if stripped[-1] in "。！？.!?":
        return stripped[:-1] + canonical + stripped[-1]
    return stripped + canonical


def _is_front_matter_paragraph(paragraph: str, current_heading: str) -> bool:
    if re.search(r"摘要|abstract\b|关键词|keywords\b", current_heading, flags=re.IGNORECASE):
        return True
    return bool(re.match(r"^\s*(?:关键词|keywords)\s*[:：]", paragraph, flags=re.IGNORECASE))


def _is_markdown_heading(text: str) -> bool:
    return bool(re.match(r"^\s*#{1,6}\s+", text))


def _run_post_rewrite_compliance(
    rewritten: dict[str, Any],
    original: dict[str, Any],
    settings: Settings,
    run_dir: Path | None = None,
    policies: EvidencePolicies | None = None,
    run: Run | None = None,
    session: Session | None = None,
    project: Project | None = None,
) -> ComplianceResult:
    policies = policies or EvidencePolicies.from_settings("final", settings)
    before = str(original.get("manuscript") or "")
    after = str(rewritten.get("manuscript") or "")
    if Counter(drafter._extract_inline_citations(before)) != Counter(
        drafter._extract_inline_citations(after),
    ):
        return ComplianceResult.fail("cite_marker_multiset_change")

    claim_map = _claim_records(rewritten.get("claim_map"))
    original_claim_map = _claim_records(original.get("claim_map"))
    if settings.final_rewrite_holistic:
        holistic_compliance = _run_holistic_surface_compliance(
            before=before,
            after=after,
            claim_map=claim_map,
            run_dir=run_dir,
            settings=settings,
            project=project,
            session=session,
        )
        if holistic_compliance.failed:
            return holistic_compliance
    else:
        coherence_reason = drafter._validate_global_coherence_output(before=before, after=after)
        # The drafter coherence validator also rejects fewer citation-bearing
        # paragraphs. Legacy final rewrite is explicitly allowed to merge nearby
        # arguments, so keep the shared hard checks but do not treat a merge
        # as a policy failure here.
        if (
            coherence_reason is not None
            and coherence_reason != "citation_bearing_paragraph_deleted"
        ):
            return ComplianceResult.fail(
                "global_coherence_validation_failed",
                [{"reason": coherence_reason}],
            )

    if len(claim_map) > max(0, int(len(original_claim_map) * 1.5)):
        return ComplianceResult.fail(
            "claim_count_explosion",
            [{"original_count": len(original_claim_map), "rewritten_count": len(claim_map)}],
        )

    whitelist = _shortlist_source_ids(run_dir) if run_dir is not None else set()
    if whitelist:
        source_bound_claims = [
            claim for claim in claim_map if _evidence_status(claim) == "source_bound"
        ]
        whitelist_errors = drafter._citation_whitelist_errors(
            {"claim_map": source_bound_claims, "prose": after},
            whitelist,
        )
        model_backed_errors = _model_backed_source_errors(claim_map)
        if whitelist_errors or model_backed_errors:
            return ComplianceResult.fail(
                "claim_grounding_failed",
                [{"error": error} for error in [*whitelist_errors, *model_backed_errors]],
            )

    citation_resolution_errors = _cite_marker_resolution_errors(after, run_dir)
    if citation_resolution_errors:
        return ComplianceResult.fail(
            "cite_marker_gate_failed",
            [{"error": error} for error in citation_resolution_errors],
        )

    grounding = _claim_grounding_diagnostic(claim_map, run_dir)
    weak_count = grounding.get("weakly_grounded_count")
    if isinstance(weak_count, int) and weak_count > 0:
        return ComplianceResult.fail(
            "claim_grounding_failed",
            [grounding],
        )

    conclusion_errors = _conclusion_supported_claim_errors(
        rewritten_claims=claim_map,
        original_claims=original_claim_map,
        original_text=before,
        rewritten_text=after,
    )
    if conclusion_errors:
        if policies.whitelist == "strict":
            return ComplianceResult.fail("evidence_whitelist_failed", conclusion_errors)
        if policies.whitelist == "soft":
            if run is not None and session is not None:
                append_event(
                    session,
                    run,
                    "evidence_whitelist_warning",
                    {
                        "phase": "final_rewrite",
                        "phase_mode": policies.phase,
                        "details": conclusion_errors,
                    },
                )
            return ComplianceResult.pass_()

    return ComplianceResult.pass_()


def _run_holistic_surface_compliance(
    *,
    before: str,
    after: str,
    claim_map: Sequence[Mapping[str, object]],
    run_dir: Path | None,
    settings: Settings,
    project: Project | None,
    session: Session | None,
) -> ComplianceResult:
    before_paragraphs = _paragraphs(before)
    after_paragraphs = _paragraphs(after)
    if len(before_paragraphs) != len(after_paragraphs):
        return ComplianceResult.fail(
            "holistic_paragraph_count_changed",
            [{"before": len(before_paragraphs), "after": len(after_paragraphs)}],
        )

    before_sequence = drafter._extract_inline_citations(before)
    after_sequence = drafter._extract_inline_citations(after)
    if before_sequence != after_sequence:
        return ComplianceResult.fail(
            "holistic_cite_marker_sequence_changed",
            [{"before": before_sequence, "after": after_sequence}],
        )

    before_by_paragraph = [
        drafter._extract_inline_citations(paragraph) for paragraph in before_paragraphs
    ]
    after_by_paragraph = [
        drafter._extract_inline_citations(paragraph) for paragraph in after_paragraphs
    ]
    if before_by_paragraph != after_by_paragraph:
        return ComplianceResult.fail(
            "holistic_paragraph_citation_sequence_changed",
            [
                {
                    "index": index,
                    "before": before_markers,
                    "after": after_markers,
                }
                for index, (before_markers, after_markers) in enumerate(
                    zip(before_by_paragraph, after_by_paragraph, strict=True),
                )
                if before_markers != after_markers
            ][:5],
        )

    if drafter._extract_cnki_section_titles(before) != drafter._extract_cnki_section_titles(after):
        return ComplianceResult.fail(
            "holistic_cnki_section_titles_changed",
            [
                {
                    "before": drafter._extract_cnki_section_titles(before),
                    "after": drafter._extract_cnki_section_titles(after),
                }
            ],
        )

    before_refs = drafter._extract_cnki_block(before, "参考文献")
    after_refs = drafter._extract_cnki_block(after, "参考文献")
    if (before_refs is None) != (after_refs is None):
        return ComplianceResult.fail("holistic_reference_block_presence_changed")
    if (
        before_refs is not None
        and after_refs is not None
        and drafter._normalize_for_block_compare(before_refs)
        != drafter._normalize_for_block_compare(after_refs)
    ):
        return ComplianceResult.fail("holistic_reference_block_modified")

    if len(after) < len(before) * 0.7:
        return ComplianceResult.fail("manuscript_shrank_too_much")

    alignment_errors = _holistic_claim_map_alignment_errors(after, claim_map, run_dir)
    if alignment_errors:
        return ComplianceResult.fail("holistic_claim_map_alignment_failed", alignment_errors)

    ngram_overlaps = _holistic_ngram_overlaps(
        after,
        settings=settings,
        project=project,
        session=session,
    )
    if ngram_overlaps:
        return ComplianceResult.fail(
            "ngram_guard_failed",
            [{"overlaps": ngram_overlaps}],
        )

    return ComplianceResult.pass_()


def _holistic_claim_map_alignment_errors(
    text: str,
    claim_map: Sequence[Mapping[str, object]],
    run_dir: Path | None,
) -> list[dict[str, object]]:
    if run_dir is None:
        return []
    cited_sources = _cited_sources_for_rewrite(run_dir)
    if not cited_sources:
        return []
    source_to_tag = {
        str(getattr(source, "source_id", "")): f"[{index}]"
        for index, source in enumerate(cited_sources, 1)
        if str(getattr(source, "source_id", ""))
    }
    expected_by_claim_paragraph = _claim_map_paragraph_tags_for_rewrite(claim_map, source_to_tag)
    if not expected_by_claim_paragraph:
        return []
    manuscript_paragraphs = _claim_alignment_paragraphs(text)
    errors: list[dict[str, object]] = []
    for index, expected_tags in enumerate(expected_by_claim_paragraph):
        if not expected_tags:
            continue
        if index >= len(manuscript_paragraphs):
            errors.append(
                {
                    "index": index,
                    "expected_tags": list(expected_tags),
                    "error": "missing_manuscript_paragraph",
                },
            )
            continue
        markers = drafter._extract_inline_citations(manuscript_paragraphs[index])
        missing = [tag for tag in expected_tags if tag not in markers]
        if missing:
            errors.append(
                {
                    "index": index,
                    "expected_tags": list(expected_tags),
                    "actual_tags": markers,
                    "missing_tags": missing,
                },
            )
    return errors[:10]


def _claim_alignment_paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    current_heading = ""
    in_references = False
    for paragraph in text.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if _is_markdown_heading(stripped):
            current_heading = stripped
            if re.search(r"参考文献|references\b", stripped, flags=re.IGNORECASE):
                in_references = True
            continue
        if in_references or _is_front_matter_paragraph(stripped, current_heading):
            continue
        paragraphs.append(paragraph)
    return paragraphs


def _holistic_ngram_overlaps(
    text: str,
    *,
    settings: Settings,
    project: Project | None,
    session: Session | None,
) -> list[str]:
    if project is None or session is None:
        return []
    from autoessay.agents import stylist as stylist_agent
    from autoessay.style_profile import build_style_profile

    profile = build_style_profile(
        session,
        project,
        allow_prior_text=settings.allow_prior_text,
    )
    return stylist_agent._five_gram_overlaps(text, profile.short_local_examples)


def _complete_success(
    *,
    run: Run,
    session: Session,
    rewrite_dir: Path,
    summary: dict[str, object],
    compliance: ComplianceResult,
    draft_version: str,
    stub: bool,
) -> dict[str, object]:
    rewrite_summary = {
        "rewrite_version": rewrite_dir.name,
        "rewrite_audit_path": f"rewrite/{rewrite_dir.name}/audit.json",
        "rewrite_diff_summary": summary,
    }
    payload = {
        "phase": "final_rewrite",
        "draft_version": draft_version,
        "rewrite_version": rewrite_dir.name,
        "stub": stub,
        "compliance_failed": compliance.failed,
        "next_stage": "critic",
        "rewrite_summary": rewrite_summary,
    }
    transition(
        run,
        "CRITIC_RUNNING",
        session,
        reason="Final rewrite completed",
        payload=payload,
    )
    append_event(session, run, "rewrite_completed", payload)
    append_event(session, run, "phase_done", payload)
    session.commit()
    return {"run_id": run.id, "state": run.state, **payload}


def _fail(
    run: Run,
    session: Session,
    state: str,
    guidance: str,
    *,
    failure_class: str,
    details: Sequence[Mapping[str, object]] | None = None,
    rewrite_version: str | None = None,
) -> dict[str, object]:
    if run.state != state:
        transition(
            run,
            state,
            session,
            reason="Final rewrite failed",
            payload={
                "phase": "final_rewrite",
                "guidance": guidance,
                "failure_class": failure_class,
                "compliance_reason": guidance if failure_class == "failed_policy" else None,
                "rewrite_version": rewrite_version,
            },
        )
    payload = {
        "phase": "final_rewrite",
        "failure_class": failure_class,
        "guidance": guidance,
        "compliance_reason": guidance if failure_class == "failed_policy" else None,
        "details": [dict(item) for item in (details or [])],
        "rewrite_version": rewrite_version,
    }
    append_event(session, run, "phase_failed", payload)
    session.commit()
    return {"run_id": run.id, "state": run.state, **payload}


def _load_original_payload(draft_dir: Path) -> dict[str, Any]:
    return {
        "manuscript": _read_optional_text(draft_dir / "style" / "paper_styled.md"),
        "claim_map": _load_jsonl_objects(draft_dir / "claim_map.jsonl"),
    }


def _latest_draft_dir(run_dir: Path) -> Path | None:
    drafts_dir = run_dir / "drafts"
    if not drafts_dir.exists():
        return None
    candidates = [path for path in drafts_dir.glob("v[0-9][0-9][0-9]") if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.name)[-1]


def _next_rewrite_dir(run_dir: Path) -> Path:
    rewrite_root = run_dir / "rewrite"
    highest = 0
    if rewrite_root.exists():
        for child in rewrite_root.iterdir():
            match = re.fullmatch(r"v(\d{3})", child.name)
            if match and child.is_dir():
                highest = max(highest, int(match.group(1)))
    return rewrite_root / f"v{highest + 1:03d}"


def _claim_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, RewriteClaim):
            records.append(dict(item.dict()))
        elif isinstance(item, Mapping):
            records.append({key: value for key, value in item.items() if isinstance(key, str)})
    return records


def _evidence_status(claim: Mapping[str, object]) -> EvidenceStatus:
    status = claim.get("evidence_status")
    if status == "model_backed":
        return "model_backed"
    return "source_bound"


def _source_ids(value: object) -> list[str]:
    if isinstance(value, str):
        return [] if value == "[UNCITED]" else [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item and item != "[UNCITED]"]
    return []


def _model_backed_source_errors(claims: Sequence[Mapping[str, object]]) -> list[str]:
    errors: list[str] = []
    for index, claim in enumerate(claims):
        if _evidence_status(claim) != "model_backed":
            continue
        if _source_ids(claim.get("source_ids")):
            errors.append(f"claim_map[{index}] model_backed claim must not carry source_ids")
    return errors


def _shortlist_source_ids(run_dir: Path | None) -> set[str]:
    if run_dir is None:
        return set()
    raw = _load_json_array(run_dir / "sources" / "shortlist.json")
    source_ids: set[str] = set()
    for item in raw:
        if isinstance(item, Mapping):
            source_id = item.get("source_id")
            if isinstance(source_id, str) and source_id:
                source_ids.add(source_id)
    return source_ids


def _cited_source_count(run_dir: Path | None) -> int | None:
    if run_dir is None:
        return None
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        return None
    metadata = _load_json_mapping(draft_dir / "draft_metadata.json")
    cited = metadata.get("cited_sources")
    return len(cited) if isinstance(cited, list) else None


def _cite_marker_resolution_errors(text: str, run_dir: Path | None) -> list[str]:
    errors: list[str] = []
    if "[UNCITED]" in text:
        errors.append('manuscript contains literal "[UNCITED]"')
    cited_count = _cited_source_count(run_dir)
    if cited_count is None:
        return errors
    for marker in drafter._extract_inline_citations(text):
        number = int(marker.strip("[]"))
        if number < 1 or number > cited_count:
            errors.append(f"citation marker {marker} does not resolve to cited_sources")
    return errors


def _claim_grounding_diagnostic(
    claim_map: Sequence[Mapping[str, object]],
    run_dir: Path | None,
) -> dict[str, object]:
    if run_dir is None:
        return {"weakly_grounded_count": 0, "weakly_grounded_claims": []}
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    if not shortlist:
        return {"weakly_grounded_count": 0, "weakly_grounded_claims": []}
    sections_by_id: dict[str, list[dict[str, object]]] = {}
    for claim in claim_map:
        section_id_raw = claim.get("section_id")
        paragraph_id = str(claim.get("paragraph_id") or "")
        section_id = (
            section_id_raw
            if isinstance(section_id_raw, str) and section_id_raw
            else paragraph_id.split("-p", 1)[0]
            if "-p" in paragraph_id
            else "rewrite"
        )
        sections_by_id.setdefault(section_id, []).append(dict(claim))
    drafted_sections = [
        DraftedSection(
            section_id=section_id,
            title=section_id,
            prose="",
            claim_map=claims,
            failed=False,
            warnings=[],
            word_count=0,
            target_words=0,
        )
        for section_id, claims in sections_by_id.items()
    ]
    return drafter._check_claim_grounding(
        drafted_sections=drafted_sections,
        shortlist=shortlist,
        run_dir=run_dir,
    )


def _conclusion_supported_claim_errors(
    *,
    rewritten_claims: Sequence[Mapping[str, object]],
    original_claims: Sequence[Mapping[str, object]],
    original_text: str,
    rewritten_text: str,
) -> list[dict[str, object]]:
    errors: list[dict[str, object]] = []
    new_terms = _new_protected_terms(original_text, rewritten_text)
    if new_terms:
        errors.append({"reason": "new_protected_terms", "terms": sorted(new_terms)[:20]})
    supported_text = " ".join(
        str(claim.get("claim_text") or "")
        for claim in original_claims
        if _source_ids(claim.get("source_ids"))
    )
    supported_tokens = _content_tokens(supported_text)
    conclusion_claims = [
        claim
        for claim in rewritten_claims
        if _is_conclusion_claim(claim) and _evidence_status(claim) == "source_bound"
    ]
    for claim in conclusion_claims:
        claim_text = str(claim.get("claim_text") or "")
        tokens = _content_tokens(claim_text)
        if tokens and supported_tokens and len(tokens & supported_tokens) / len(tokens) < 0.25:
            errors.append(
                {
                    "reason": "conclusion_claim_outside_supported_digest",
                    "paragraph_id": claim.get("paragraph_id"),
                    "claim_text": claim_text[:200],
                },
            )
    return errors


def _is_conclusion_claim(claim: Mapping[str, object]) -> bool:
    values = [
        claim.get("section_id"),
        claim.get("section_title"),
        claim.get("paragraph_id"),
    ]
    joined = " ".join(str(value).lower() for value in values if isinstance(value, str))
    return "conclusion" in joined or "结论" in joined


def _new_protected_terms(original_text: str, rewritten_text: str) -> set[str]:
    original_clean = _strip_citations(original_text)
    rewritten_clean = _strip_citations(rewritten_text)
    original_terms = _protected_terms(original_clean)
    rewritten_terms = _protected_terms(rewritten_clean)
    return rewritten_terms - original_terms


def _protected_terms(text: str) -> set[str]:
    terms: set[str] = set()
    terms.update(re.findall(r"\b(?:1[5-9]\d{2}|20\d{2})\b", text))
    terms.update(re.findall(r"《[^》]{2,80}》", text))
    terms.update(re.findall(r"\"[^\"]{3,80}\"", text))
    terms.update(re.findall(r"\b[A-Z][A-Za-z]+(?:[ \t]+[A-Z][A-Za-z]+){1,5}\b", text))
    # Percent/statistical values after citation markers are removed.
    terms.update(re.findall(r"\b\d+(?:\.\d+)?\s*(?:%|percent|percentage)\b", text, re.I))
    return {term.strip() for term in terms if term.strip()}


def _strip_citations(text: str) -> str:
    return re.sub(r"\[\d+\]", "", text)


_CONTENT_STOPWORDS = {
    "the",
    "and",
    "that",
    "with",
    "from",
    "this",
    "into",
    "their",
    "have",
    "has",
    "were",
    "was",
    "are",
    "paper",
    "claim",
    "section",
}


def _content_tokens(text: str) -> set[str]:
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z]{3,}|[一-鿿]{2,}", text)
        if token.lower() not in _CONTENT_STOPWORDS
    }
    return tokens


def _diff_summary(
    before: str,
    after: str,
    before_claims: Sequence[Mapping[str, object]],
    after_claims: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "paragraphs_reordered": _paragraphs_reordered(before, after),
        "transitions_added": _transitions_added(before, after),
        "claims_consolidated": max(0, len(before_claims) - len(after_claims)),
        "claim_map_count": len(after_claims),
    }


def _paragraphs_reordered(before: str, after: str) -> int:
    before_paragraphs = _paragraphs(before)
    after_paragraphs = _paragraphs(after)
    before_positions = {
        _normalize_paragraph(paragraph): i for i, paragraph in enumerate(before_paragraphs)
    }
    moved = 0
    for index, paragraph in enumerate(after_paragraphs):
        key = _normalize_paragraph(paragraph)
        original_index = before_positions.get(key)
        if original_index is not None and original_index != index:
            moved += 1
    return moved


def _transitions_added(before: str, after: str) -> int:
    markers = (
        "however",
        "therefore",
        "moreover",
        "nevertheless",
        "因此",
        "然而",
        "同时",
        "进一步",
        "换言之",
    )
    before_count = sum(before.lower().count(marker) for marker in markers)
    after_count = sum(after.lower().count(marker) for marker in markers)
    return max(0, after_count - before_count)


def _paragraphs(text: str) -> list[str]:
    return [paragraph for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]


def _normalize_paragraph(paragraph: str) -> str:
    return re.sub(r"\s+", " ", paragraph).strip()


def _unified_diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="stylist/manuscript.md",
            tofile="rewrite/manuscript.md",
        ),
    )


def _read_sources_json(path: Path) -> list[Any]:
    from autoessay.clients.common import NormalizedSource

    records = _load_json_array(path)
    sources: list[Any] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        try:
            sources.append(NormalizedSource.parse_obj(record))
        except ValidationError:
            continue
    return sources


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                records.append(
                    {key: value for key, value in decoded.items() if isinstance(key, str)}
                )
    return records


def _load_json_array(path: Path) -> list[object]:
    if not path.exists():
        return []
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _load_json_array_of_objects(path: Path) -> list[dict[str, object]]:
    records = _load_json_array(path)
    return [
        {key: value for key, value in item.items() if isinstance(key, str)}
        for item in records
        if isinstance(item, dict)
    ]


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


__all__ = [
    "ComplianceResult",
    "FinalRewriteOutput",
    "RewriteArtifact",
    "complete_downstream_review_fallback",
    "latest_rewrite_dir",
    "latest_rewrite_summary",
    "load_latest_rewrite_artifact",
    "rewrite_summary_for_run",
    "rewrite_summary_for_run_dir",
    "run_final_rewrite",
    "run_final_rewrite_then_critic",
    "_run_post_rewrite_compliance",
]
