"""Independent north-star gate judge for paired manuscript evaluation.

This module is intentionally separate from both polish-loop critics:
it measures the selected pipeline manuscript against the frozen
baseline, but it never drives rewrites.
"""

from __future__ import annotations

import random
from math import ceil
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictBool, StrictStr

NORTH_STAR_GATE_SCHEMA_VERSION = "paired_blind_box_ledger_v1"

NORTH_STAR_GATE_ITEM_MAX: dict[str, float] = {
    "citation_alignment": 4.0,
    "no_sentinels": 2.0,
    "cnki_format": 2.0,
    "academic_voice": 2.0,
    "new_material": 2.0,
    "new_perspective": 2.0,
    "new_method": 2.0,
    "new_question": 2.0,
    "new_argument": 2.0,
    "eight_sections": 3.0,
    "claim_evidence_conclusion": 2.0,
    "abstract_keywords_refs": 2.0,
    "cross_section_coherence": 3.0,
}

NORTH_STAR_GATE_ITEM_IDS = tuple(NORTH_STAR_GATE_ITEM_MAX.keys())

NORTH_STAR_GATE_SYSTEM_PROMPT = """你是顶级人文社科期刊的盲评评审人。
你将看到两份候选稿，分别标记为 A 和 B。
你不知道哪份是哪种来源，也不需要知道。

任务：按 box-checking 加分制独立打分，给出每个子项的得分 + 证据 + reason_code。
不输出比较结论；不输出整体偏好；只输出结构化 JSON。

子项 + 满分（写死，不可改）：
- 合规性 compliance（10 分）：
  - citation_alignment（4 分）：引用与参考文献列表对齐
  - no_sentinels（2 分）：无 [UNCITED] / TODO_EVIDENCE / 非规范化 cite marker
  - cnki_format（2 分）：体例完整（八节正文+摘要+关键词+参考文献）
  - academic_voice（2 分）：中文学术语态自然，无翻译腔/AI 套话

- 创新性 novelty（10 分）：5 类创新源各 2 分，命中需正文有可定位证据
  - new_material（2 分）
  - new_perspective（2 分）
  - new_method（2 分）
  - new_question（2 分）
  - new_argument（2 分）

- 完整性 completeness（10 分）：
  - eight_sections（3 分）：8 节 body 全有且每节 ≥1200 字
  - claim_evidence_conclusion（2 分）：论点-论据-结论结构完整
  - abstract_keywords_refs（2 分）：摘要/关键词/参考文献完整
  - cross_section_coherence（3 分）：跨节连贯、首尾呼应、无自相矛盾

每个 item 必须给：
- points（0 到 max，可半分）
- reason_code: SUPPORTED | PARTIAL | WEAK | INVALID
- evidence: 1-3 条 manuscript 内 short quote 或 anchor
  （如 "S03_P02 句..." / "MISSING: ..." / "REF[7]"）
- brief_reason: 一句话
- 顶层 score 必须严格等于该稿 13 个 item points 的算术和；输出前必须重新相加核对

只输出严格 JSON。

输出 schema:
{
  "schema_version": "paired_blind_box_ledger_v1",
  "validity": {"can_score": true, "reason": null},
  "scores": {
    "A": {
      "score": 0,
      "items": [
        {
          "id": "citation_alignment",
          "max": 4,
          "points": 0,
          "reason_code": "...",
          "evidence": [...],
          "brief_reason": "..."
        },
        "...所有 13 项"
      ]
    },
    "B": {"score": 0, "items": []}
  }
}

约束：
- 不输出比较语 "A 比 B 好"
- 评分仅依据 manuscript 内可定位证据
- 不参考标题暗示或外部知识补证
- 不虚构材料 / 数据 / 引用 / 创新点
- 没证据就按 INVALID/缺失处理
"""

NORTH_STAR_GATE_USER_TEMPLATE = """请按 system 规则独立评 A 和 B。只输出 JSON。

manuscript_A:
{{manuscript_A}}

manuscript_B:
{{manuscript_B}}
"""


ReasonCode = Literal["SUPPORTED", "PARTIAL", "WEAK", "INVALID"]


class NorthStarGateItem(BaseModel):
    id: StrictStr
    max: float
    points: float
    reason_code: ReasonCode
    evidence: list[StrictStr] = Field(default_factory=list)
    brief_reason: StrictStr = ""

    class Config:
        extra = "allow"


class NorthStarGateSideScore(BaseModel):
    score: float
    items: list[NorthStarGateItem] = Field(default_factory=list)

    class Config:
        extra = "allow"


class NorthStarGateValidity(BaseModel):
    can_score: StrictBool = True
    reason: StrictStr | None = None

    class Config:
        extra = "allow"


class NorthStarGateOutput(BaseModel):
    schema_version: StrictStr = NORTH_STAR_GATE_SCHEMA_VERSION
    validity: NorthStarGateValidity = Field(default_factory=NorthStarGateValidity)
    scores: dict[StrictStr, NorthStarGateSideScore] = Field(default_factory=dict)

    class Config:
        extra = "allow"


def build_north_star_gate_user_prompt(
    *,
    manuscript_a: str,
    manuscript_b: str,
) -> str:
    return NORTH_STAR_GATE_USER_TEMPLATE.replace("{{manuscript_A}}", manuscript_a).replace(
        "{{manuscript_B}}", manuscript_b
    )


def coin_flip_slots(rng: random.Random | random.SystemRandom | None = None) -> tuple[str, str]:
    """Return ``(pipeline_slot, baseline_slot)`` for one blind sample."""
    chooser = rng if rng is not None else random.SystemRandom()
    if chooser.choice([False, True]):
        return "A", "B"
    return "B", "A"


def validate_gate_output(output: NorthStarGateOutput) -> dict[str, Any]:
    """Backend validation independent of the LLM's self-reported validity."""
    structural_errors: list[str] = []
    checksum_errors: list[str] = []
    checksum_failed = False
    corrected_scores: dict[str, float] = {}
    if output.schema_version != NORTH_STAR_GATE_SCHEMA_VERSION:
        structural_errors.append(f"schema_version:{output.schema_version}")
    if not output.validity.can_score:
        structural_errors.append(f"llm_validity:{output.validity.reason or 'can_score_false'}")
    for side in ("A", "B"):
        score = output.scores.get(side)
        if score is None:
            structural_errors.append(f"missing_scores:{side}")
            continue
        items_by_id: dict[str, NorthStarGateItem] = {}
        duplicate_ids: set[str] = set()
        for item in score.items:
            if item.id in items_by_id:
                duplicate_ids.add(item.id)
            items_by_id[item.id] = item
        missing = [item_id for item_id in NORTH_STAR_GATE_ITEM_IDS if item_id not in items_by_id]
        extra = sorted(set(items_by_id) - set(NORTH_STAR_GATE_ITEM_IDS))
        if missing:
            structural_errors.append(f"{side}:missing_items:{','.join(missing)}")
        if extra:
            structural_errors.append(f"{side}:extra_items:{','.join(extra)}")
        if duplicate_ids:
            structural_errors.append(f"{side}:duplicate_items:{','.join(sorted(duplicate_ids))}")
        item_sum = 0.0
        for item_id in NORTH_STAR_GATE_ITEM_IDS:
            ledger_item = items_by_id.get(item_id)
            if ledger_item is None:
                continue
            expected_max = NORTH_STAR_GATE_ITEM_MAX[item_id]
            if abs(float(ledger_item.max) - expected_max) > 0.001:
                structural_errors.append(f"{side}:{item_id}:max_mismatch:{ledger_item.max}")
            if float(ledger_item.points) < 0 or float(ledger_item.points) - expected_max > 0.001:
                structural_errors.append(
                    f"{side}:{item_id}:points_out_of_range:{ledger_item.points}"
                )
            if not ledger_item.evidence:
                structural_errors.append(f"{side}:{item_id}:missing_evidence")
            item_sum += float(ledger_item.points)
        corrected_scores[side] = item_sum
        if abs(item_sum - float(score.score)) > 0.001:
            checksum_failed = True
            checksum_errors.append(f"{side}:checksum:{score.score}!={item_sum}")
    errors = [*structural_errors, *checksum_errors]
    return {
        "can_score": not structural_errors,
        "checksum_failed": checksum_failed,
        "checksum_corrected": checksum_failed and not structural_errors,
        "validation_errors": errors,
        "structural_errors": structural_errors,
        "checksum_errors": checksum_errors,
        "corrected_scores": corrected_scores,
    }


def evaluate_gate_sample(
    *,
    output: NorthStarGateOutput,
    pipeline_slot: str,
    baseline_slot: str,
) -> dict[str, Any]:
    validation = validate_gate_output(output)
    raw = output.dict()
    sample: dict[str, Any] = {
        "can_score": bool(validation["can_score"]),
        "checksum_failed": bool(validation["checksum_failed"]),
        "checksum_corrected": bool(validation["checksum_corrected"]),
        "validation_errors": validation["validation_errors"],
        "structural_errors": validation["structural_errors"],
        "checksum_errors": validation["checksum_errors"],
        "coin": [f"{pipeline_slot}=pipeline", f"{baseline_slot}=baseline"],
        "pipeline_slot": pipeline_slot,
        "baseline_slot": baseline_slot,
        "raw": raw,
    }
    if not sample["can_score"]:
        return sample

    pipeline_scores = output.scores[pipeline_slot]
    baseline_scores = output.scores[baseline_slot]
    corrected_scores = validation["corrected_scores"]
    pipeline_items = {item.id: item for item in pipeline_scores.items}
    baseline_items = {item.id: item for item in baseline_scores.items}
    item_deltas = {
        item_id: float(pipeline_items[item_id].points) - float(baseline_items[item_id].points)
        for item_id in NORTH_STAR_GATE_ITEM_IDS
    }
    max_loss = min(item_deltas.values())
    pipeline_score = float(corrected_scores[pipeline_slot])
    baseline_score = float(corrected_scores[baseline_slot])
    sample.update(
        {
            "pipeline_score": pipeline_score,
            "baseline_score": baseline_score,
            "reported_pipeline_score": float(pipeline_scores.score),
            "reported_baseline_score": float(baseline_scores.score),
            "total_delta": pipeline_score - baseline_score,
            "item_deltas": item_deltas,
            "max_loss": max_loss,
        }
    )
    return sample


def should_resample_gate(first_sample: dict[str, Any]) -> bool:
    if not first_sample.get("can_score"):
        return True
    if first_sample.get("checksum_failed"):
        return True
    max_loss = float(first_sample.get("max_loss", 0.0))
    total_delta = float(first_sample.get("total_delta", 0.0))
    return (-1.5 < max_loss < -0.5) or abs(total_delta) <= 1.0


def aggregate_gate_samples(
    samples: list[dict[str, Any]],
    *,
    forced_samples: int | None = None,
) -> dict[str, Any]:
    valid = [sample for sample in samples if sample.get("can_score")]
    required_valid = 1
    if forced_samples is not None and forced_samples > 0:
        required_valid = ceil(forced_samples / 2)
    if not valid:
        return {
            "pass": False,
            "reason": "gate_unscorable",
            "max_loss": None,
            "median_item_delta": {},
            "n_samples": len(samples),
            "n_valid_samples": 0,
            "n_required_valid_samples": required_valid,
            "forced_samples": forced_samples,
            "samples": samples,
        }
    if len(valid) < required_valid:
        return {
            "pass": False,
            "reason": "insufficient_valid_gate_samples",
            "max_loss": None,
            "median_item_delta": {},
            "n_samples": len(samples),
            "n_valid_samples": len(valid),
            "n_required_valid_samples": required_valid,
            "forced_samples": forced_samples,
            "samples": samples,
        }

    median_item_delta = {
        item_id: float(median([float(sample["item_deltas"][item_id]) for sample in valid]))
        for item_id in NORTH_STAR_GATE_ITEM_IDS
    }
    max_loss = min(median_item_delta.values())
    return {
        "pass": max_loss >= -1.0,
        "reason": None,
        "max_loss": max_loss,
        "median_item_delta": median_item_delta,
        "n_samples": len(samples),
        "n_valid_samples": len(valid),
        "n_required_valid_samples": required_valid,
        "forced_samples": forced_samples,
        "samples": samples,
    }


__all__ = [
    "NORTH_STAR_GATE_ITEM_IDS",
    "NORTH_STAR_GATE_ITEM_MAX",
    "NORTH_STAR_GATE_SCHEMA_VERSION",
    "NORTH_STAR_GATE_SYSTEM_PROMPT",
    "NORTH_STAR_GATE_USER_TEMPLATE",
    "NorthStarGateOutput",
    "aggregate_gate_samples",
    "build_north_star_gate_user_prompt",
    "coin_flip_slots",
    "evaluate_gate_sample",
    "should_resample_gate",
    "validate_gate_output",
]
