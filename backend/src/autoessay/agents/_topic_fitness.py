"""Scout-side source pool topic fitness filtering."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from autoessay.agents.scout import _extract_keywords, _gather_kernel_concept_keywords
from autoessay.clients.common import NormalizedSource

_DOMAIN_HOMOPHONE_BAN_SEED: Mapping[str, list[str]] = {
    "financial_history": [
        # "布雷顿森林" 同音/同字误命中
        "Brayton",
        "布雷顿循环",
        "supercritical CO2",
        "超临界 CO2",
        "热力循环",
        # 常见学术噪声主题（R10 实测）
        "森林管护",
        "森林经营",
        "林业",
        "森林资源",
        "乳腺癌",
        "Breast Cancer",
        "PS4-05",
        "PS3-05",
        "PS2-05",
        "PS1-05",
        "犯罪记录",
        "刑事记录封存",
        "护理",
        "ICU",
        "建筑施工",
        "暖通空调",
    ],
    # 其他 domain 占位，后续 slice 扩展
    "economic_history": [],
    "literary_history": [],
    "general_academic": [],
}

_KEYWORD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "布雷顿": ("bretton",),
    "森林": ("woods",),
    "金本位": ("gold", "standard"),
    "黄金": ("gold",),
    "美元": ("dollar", "dollars", "usd"),
    "美联储": ("federal", "reserve", "fed"),
    "会议纪要": ("minutes",),
    "备忘录": ("memorandum", "memo"),
    "制度": ("institutional", "institutions"),
    "约束": ("constraint", "constraints"),
    "约束力": ("constraint", "constraints"),
    "可兑换": ("convertibility", "convertible"),
    "兑换": ("convertibility", "convertible", "exchange"),
    "失效": ("collapse", "breakdown"),
    "失效节点": ("collapse", "breakdown"),
    "断裂": ("breakdown", "collapse"),
    "黄金池": ("gold", "pool"),
    "bretton": ("布雷顿",),
    "woods": ("森林",),
    "gold": ("黄金", "金本位"),
    "standard": ("金本位",),
    "dollar": ("美元",),
    "dollars": ("美元",),
    "usd": ("美元",),
    "imf": ("国际货币基金组织",),
}


@dataclass
class TopicFitnessResult:
    kept: list[NormalizedSource]
    dropped: list[dict[str, object]]
    drop_reasons: dict[str, int]
    audit: dict[str, object]


@dataclass(frozen=True)
class _CandidateDecision:
    source: NormalizedSource
    entity_match: set[str]
    concept_match: set[str]
    reason: str
    ban_term: str | None = None


def filter_off_topic_candidates(
    candidates: Sequence[NormalizedSource],
    *,
    title: str,
    research_kernel: Mapping[str, object] | None,
    proposal: Mapping[str, object] | None = None,
    domain_data: Mapping[str, object] | None = None,
    min_pool: int = 5,
) -> TopicFitnessResult:
    """Drop off-topic candidates from scout's raw collection.

    The default path is an entity + concept AND-gate over title and
    ``research_kernel`` keyword buckets. When the kernel concept bucket
    is empty, proposal keywords/research_question become the concept
    fallback. The filter no longer degrades open merely because one
    bucket is missing; it falls back to a single-bucket anchor instead.
    """
    title_keywords = _expand_keywords(_extract_keywords(title))
    kernel_concept_keywords = _expand_keywords(_gather_kernel_concept_keywords(research_kernel))
    proposal_concept_keywords = _expand_keywords(_gather_proposal_concept_keywords(proposal))
    raw_concept_keywords = (
        kernel_concept_keywords if kernel_concept_keywords else proposal_concept_keywords
    )
    concept_keywords = raw_concept_keywords - title_keywords
    candidate_count = len(candidates)
    ban_terms = _homophone_ban_terms(domain_data)

    warnings: list[str] = []
    if not kernel_concept_keywords and proposal_concept_keywords:
        warnings.append("concept_bucket_from_proposal")
    if not title_keywords:
        warnings.append("missing_entity_bucket")
    if not concept_keywords:
        warnings.append("missing_concept_bucket")

    use_single_bucket = not title_keywords or not concept_keywords
    single_bucket_keywords = title_keywords or concept_keywords

    strong_kept: list[NormalizedSource] = []
    rescue_candidates: list[_CandidateDecision] = []
    dropped_decisions: list[_CandidateDecision] = []

    for source in candidates:
        source_keywords = _expand_keywords(_extract_keywords(_source_match_text(source)))
        entity_match = title_keywords & source_keywords
        concept_match = concept_keywords & source_keywords
        ban_term = _matching_ban_term(source.title, ban_terms)
        if ban_term is not None:
            dropped_decisions.append(
                _CandidateDecision(
                    source=source,
                    entity_match=entity_match,
                    concept_match=concept_match,
                    reason="homophone_ban",
                    ban_term=ban_term,
                ),
            )
            continue
        if use_single_bucket:
            anchor_match = single_bucket_keywords & source_keywords
            if anchor_match:
                strong_kept.append(source)
            else:
                dropped_decisions.append(
                    _CandidateDecision(
                        source=source,
                        entity_match=entity_match,
                        concept_match=concept_match,
                        reason="no_anchor_overlap",
                    ),
                )
            continue
        if entity_match and concept_match:
            strong_kept.append(source)
            continue
        if len(concept_match) >= 2:
            rescue_candidates.append(
                _CandidateDecision(
                    source=source,
                    entity_match=entity_match,
                    concept_match=concept_match,
                    reason="no_overlap",
                ),
            )
            continue
        dropped_decisions.append(
            _CandidateDecision(
                source=source,
                entity_match=entity_match,
                concept_match=concept_match,
                reason="no_overlap",
            ),
        )

    kept = list(strong_kept)
    rescued: list[_CandidateDecision] = []
    remaining_rescues: list[_CandidateDecision] = []
    for decision in rescue_candidates:
        if len(kept) < min_pool:
            kept.append(_add_risk_flag(decision.source, "weak_entity_anchor"))
            rescued.append(decision)
        else:
            remaining_rescues.append(decision)

    dropped_decisions.extend(remaining_rescues)
    dropped = [_drop_record(decision) for decision in dropped_decisions]
    drop_reasons: dict[str, int] = dict(Counter(str(record["reason"]) for record in dropped))
    if len(kept) < min_pool:
        warnings.append("min_pool_triggered")
    dropped_count = len(dropped)
    drop_rate = dropped_count / candidate_count if candidate_count else 0.0
    if drop_rate >= 0.5:
        warnings.append("high_drop_rate")

    audit = _audit_payload(
        candidate_count=candidate_count,
        kept_count=len(kept),
        dropped_count=dropped_count,
        bucket_sizes={"entity": len(title_keywords), "concept": len(concept_keywords)},
        min_pool=min_pool,
        drop_reasons=drop_reasons,
        rescued_count=len(rescued),
        warnings=warnings,
        gate_mode="single_bucket_anchor" if use_single_bucket else "entity_and_concept",
    )
    return TopicFitnessResult(
        kept=kept,
        dropped=dropped,
        drop_reasons=drop_reasons,
        audit=audit,
    )


def source_pool_quality_event_needed(audit: Mapping[str, object]) -> bool:
    drop_rate = audit.get("drop_rate")
    kept_count = audit.get("kept_count")
    min_pool = audit.get("min_pool")
    return (
        isinstance(drop_rate, (int, float))
        and drop_rate >= 0.5
        or (isinstance(kept_count, int) and isinstance(min_pool, int) and kept_count < min_pool)
    )


def _source_match_text(source: NormalizedSource) -> str:
    abstract = source.abstract or ""
    return f"{source.title} {abstract[:200]}"


def _expand_keywords(keywords: set[str]) -> set[str]:
    expanded = set(keywords)
    for keyword in list(keywords):
        expanded.update(_KEYWORD_ALIASES.get(keyword, ()))
    if {"布雷顿", "森林"} <= expanded or {"bretton", "woods"} <= expanded:
        expanded.update({"布雷顿", "森林", "bretton", "woods"})
    return {keyword.casefold() for keyword in expanded if keyword}


def _homophone_ban_terms(domain_data: Mapping[str, object] | None) -> list[str]:
    domain_id = ""
    terms: list[str] = []
    if isinstance(domain_data, Mapping):
        raw_domain_id = domain_data.get("id")
        if isinstance(raw_domain_id, str):
            domain_id = raw_domain_id
        terms.extend(_string_list(domain_data.get("exclusion_terms")))
        search = domain_data.get("search")
        if isinstance(search, Mapping):
            terms.extend(_string_list(search.get("exclusion_terms")))
    terms.extend(_DOMAIN_HOMOPHONE_BAN_SEED.get(domain_id, []))
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        compact = _compact_for_contains(term)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        out.append(term)
    return out


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _gather_proposal_concept_keywords(proposal: Mapping[str, object] | None) -> set[str]:
    if not isinstance(proposal, Mapping):
        return set()
    out: set[str] = set()
    for key in ("research_question", "approach", "significance"):
        value = proposal.get(key)
        if isinstance(value, str) and value.strip():
            out.update(_extract_keywords(value))
    keywords = proposal.get("preliminary_keywords")
    if isinstance(keywords, Sequence) and not isinstance(keywords, (str, bytes)):
        for keyword in keywords:
            if isinstance(keyword, str) and keyword.strip():
                out.update(_extract_keywords(keyword))
    return out


def _matching_ban_term(title: str, ban_terms: Sequence[str]) -> str | None:
    compact_title = _compact_for_contains(title)
    if not compact_title:
        return None
    for term in ban_terms:
        if _compact_for_contains(term) in compact_title:
            return term
    return None


def _compact_for_contains(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\-_–—·:：,，.。()（）]+", "", normalized)


def _add_risk_flag(source: NormalizedSource, risk_flag: str) -> NormalizedSource:
    risk_flags = list(source.risk_flags)
    if risk_flag not in risk_flags:
        risk_flags.append(risk_flag)
    return source.copy(update={"risk_flags": risk_flags})


def _drop_record(decision: _CandidateDecision) -> dict[str, object]:
    return {
        "source_id": decision.source.source_id,
        "title": decision.source.title,
        "reason": decision.reason,
        "entity_match": sorted(decision.entity_match),
        "concept_match": sorted(decision.concept_match),
    }


def _audit_payload(
    *,
    candidate_count: int,
    kept_count: int,
    dropped_count: int,
    bucket_sizes: dict[str, int],
    min_pool: int,
    drop_reasons: dict[str, int],
    rescued_count: int,
    warnings: list[str],
    gate_mode: str,
) -> dict[str, object]:
    top_drop_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(drop_reasons.items(), key=lambda item: (-item[1], item[0]))
    ][:5]
    drop_rate = dropped_count / candidate_count if candidate_count else 0.0
    return {
        "filter": "topic_fitness_filter",
        "gate_mode": gate_mode,
        "candidate_count": candidate_count,
        "kept_count": kept_count,
        "dropped_count": dropped_count,
        "drop_rate": drop_rate,
        "bucket_sizes": bucket_sizes,
        "min_pool": min_pool,
        "min_pool_triggered": kept_count < min_pool,
        "rescued_count": rescued_count,
        "warnings": warnings,
        "top_drop_reasons": top_drop_reasons,
    }
