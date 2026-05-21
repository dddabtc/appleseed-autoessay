"""Paper-mode registry for the PR-C series intake gate.

Defines the modes a user can pick at C0 (research-kernel intake) and
which downstream features each mode requires. Hardcoded Python
registry, NOT a DB table — modes change with code (new mode requires
new drafter section plan + critic checks etc), so DB rows would
falsely imply runtime flexibility.

Layered semantics:
- ``mode_id``: stable string stored in ``runs.paper_mode`` (validated
  string, NOT a DB enum, so adding modes in later PRs doesn't require
  a schema migration).
- ``status``: lifecycle state from C0's perspective:
  - ``available``: ready for production use; user can pick freely.
  - ``developer_preview``: usable but with limitations (e.g.
    ``empirical`` ships at C0 in preview because its strict evidence
    grounding only lands in C1). User must explicitly opt in.
  - ``coming_soon``: registry knows about it but the backing PR
    hasn't shipped; UI grays it out. Backend rejects creation.
- ``requires_capability``: list of feature flags from later PRs;
  when those PRs land they flip the corresponding capability and
  modes that need them get promoted.

This module is the single source of truth. ``GET /api/paper_modes``
serializes this registry; the frontend wizard reads from there.

Round-1..5 codex consensus locked in:
- 9 tension types + extensible discipline_subtype (PR-C3 territory)
- evidence ledger (PR-C1)
- framework lens operationalized (PR-C2)
- C0 metadata on Run, snapshotted into proposal artifacts
- mode availability matrix (this file)
- legacy backfill: paper_mode="empirical" with research_kernel_json
  carrying ``legacy_backfill: true``
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaperModeSpec:
    """Single mode definition. See module docstring for semantics."""

    mode_id: str
    label_en: str
    label_zh: str
    label_ja: str
    description_en: str
    description_zh: str
    description_ja: str
    status: str  # "available" | "developer_preview" | "coming_soon"
    requires_capability: tuple[str, ...]
    drafter_section_plan: tuple[str, ...]
    permits_empirical_chapters: bool
    primary_material_required: bool


# Capability flags. Each PR in the C-series flips one of these on
# when it lands. The registry below references them by name.
CAPABILITY_EVIDENCE_LEDGER = "evidence_ledger"  # PR-C1
CAPABILITY_TENSION_TAXONOMY = "tension_taxonomy"  # PR-C3
CAPABILITY_FRAMEWORK_LENS = "framework_lens"  # PR-C2
CAPABILITY_COMPARATIVE_SCAFFOLD = "comparative_scaffold"  # PR-C4
CAPABILITY_REVIEW_ARTICLE = "review_article"  # PR-C5
CAPABILITY_THEORY_REVIEW = "theory_review"  # PR-C5b


# Default section plan reused by ``empirical`` and ``case_analysis``
# until C1+C3 ship more nuanced shapes.
_EMPIRICAL_SECTIONS = (
    "introduction",
    "historiography",
    "sources_method",
    "empirical_section_i",
    "empirical_section_ii",
    "empirical_section_iii",
    "discussion",
    "conclusion",
)

_THEORY_ARTICLE_SECTIONS = (
    "introduction",
    "conceptual_genealogy",
    "core_argument",
    "conceptual_development_1",
    "conceptual_development_2",
    "implications",
    "conclusion",
)

_REVIEW_ARTICLE_SECTIONS = (
    "introduction",
    "scope_method",
    "thematic_synthesis_1",
    "thematic_synthesis_2",
    "thematic_synthesis_3",
    "open_questions",
    "conclusion",
)

_THEORY_REVIEW_SECTIONS = (
    "introduction",
    "conceptual_landscape",
    "school_lineage_1",
    "school_lineage_2",
    "school_lineage_3",
    "open_questions",
    "conclusion",
)


# PR-257a — locale-aware section titles. The drafter consults this
# map when ``paper_language`` is zh / ja, and falls back to the
# English humanized form (``Introduction``, ``Sources & Method``,
# etc.) when the language has no entry. Real-paper run #3 surfaced
# that ``case_analysis`` zh papers were rendering ``## Introduction``
# / ``## Historiography`` because the paper_modes branch in
# ``_section_plan`` only knew how to humanize snake_case slugs into
# English titles. Codex round-2 verdict (Q3=A): keep the title
# registry next to the spec, not inside drafter.
#
# Numbering follows CNKI convention (一、二、三、…). Section IDs
# stay snake_case so the prompt registry + per-section overrides
# keyed by hyphenated slug continue to match.
_SECTION_TITLES_ZH: dict[str, str] = {
    # _EMPIRICAL_SECTIONS (case_analysis + empirical)
    "introduction": "一、引言",
    "historiography": "二、文献综述",
    "sources_method": "三、研究方法",
    "empirical_section_i": "四、案例分析（一）",
    "empirical_section_ii": "五、案例分析（二）",
    "empirical_section_iii": "六、案例分析（三）",
    "discussion": "七、讨论",
    "conclusion": "八、结论",
    # _THEORY_ARTICLE_SECTIONS extras
    "conceptual_genealogy": "二、概念谱系",
    "core_argument": "三、核心论证",
    "conceptual_development_1": "四、理论展开（一）",
    "conceptual_development_2": "五、理论展开（二）",
    "implications": "六、理论意涵",
    # _REVIEW_ARTICLE_SECTIONS extras
    "scope_method": "二、范围与方法",
    "thematic_synthesis_1": "三、主题综述（一）",
    "thematic_synthesis_2": "四、主题综述（二）",
    "thematic_synthesis_3": "五、主题综述（三）",
    "open_questions": "六、待解问题",
    # _THEORY_REVIEW_SECTIONS extras
    "conceptual_landscape": "二、概念图景",
    "school_lineage_1": "三、学派源流（一）",
    "school_lineage_2": "四、学派源流（二）",
    "school_lineage_3": "五、学派源流（三）",
}

_SECTION_TITLES_JA: dict[str, str] = {
    # _EMPIRICAL_SECTIONS
    "introduction": "一、序論",
    "historiography": "二、研究史",
    "sources_method": "三、史料と方法",
    "empirical_section_i": "四、事例分析（一）",
    "empirical_section_ii": "五、事例分析（二）",
    "empirical_section_iii": "六、事例分析（三）",
    "discussion": "七、考察",
    "conclusion": "八、結論",
    # _THEORY_ARTICLE_SECTIONS extras
    "conceptual_genealogy": "二、概念系譜",
    "core_argument": "三、核心議論",
    "conceptual_development_1": "四、理論展開（一）",
    "conceptual_development_2": "五、理論展開（二）",
    "implications": "六、理論的含意",
    # _REVIEW_ARTICLE_SECTIONS extras
    "scope_method": "二、範囲と方法",
    "thematic_synthesis_1": "三、主題総括（一）",
    "thematic_synthesis_2": "四、主題総括（二）",
    "thematic_synthesis_3": "五、主題総括（三）",
    "open_questions": "六、未解決の問題",
    # _THEORY_REVIEW_SECTIONS extras
    "conceptual_landscape": "二、概念景観",
    "school_lineage_1": "三、学派系譜（一）",
    "school_lineage_2": "四、学派系譜（二）",
    "school_lineage_3": "五、学派系譜（三）",
}

LOCALIZED_SECTION_TITLES: dict[str, dict[str, str]] = {
    "zh": _SECTION_TITLES_ZH,
    "ja": _SECTION_TITLES_JA,
}


def get_localized_section_title(section_id: str, language: str) -> str | None:
    """Return the locale-appropriate display title for a paper_modes
    section_id, or ``None`` to fall back to the English humanized
    form.

    Drafter calls this from ``_section_plan`` when paper_mode picks
    ``spec.drafter_section_plan`` (snake_case ids) and the resolved
    paper_language is zh / ja. Returning ``None`` means "no override"
    so the caller's existing ``_humanize_section_id`` fallback kicks
    in (English).
    """
    titles = LOCALIZED_SECTION_TITLES.get(language)
    if titles is None:
        return None
    return titles.get(section_id)


_REGISTRY: dict[str, PaperModeSpec] = {
    "case_analysis": PaperModeSpec(
        mode_id="case_analysis",
        label_en="Case analysis",
        label_zh="个案分析",
        label_ja="個別事例分析",
        description_en=(
            "A focused study of a specific case (event, institution, text, "
            "person, region). Empirical chapters allowed but treated as "
            "case description rather than statistical claims. Available "
            "at C0 ship — uses the existing drafter pipeline."
        ),
        description_zh=(
            "针对单一对象（事件 / 机构 / 文本 / 人物 / 地区）的聚焦研究。"
            "允许实证章节，但章节性质偏案例描述而非统计推断。"
            "C0 阶段即可使用 — 走现有 drafter 流水线。"
        ),
        description_ja=(
            "単一対象（事件・機構・テクスト・人物・地域）に焦点を絞った研究。"
            "実証章を許容するが、統計的推論ではなく事例記述として扱う。"
            "C0 リリース時点で利用可能 — 既存の起草パイプラインを使用。"
        ),
        status="available",
        requires_capability=(),
        drafter_section_plan=_EMPIRICAL_SECTIONS,
        permits_empirical_chapters=True,
        primary_material_required=False,
    ),
    "empirical": PaperModeSpec(
        mode_id="empirical",
        label_en="Empirical research",
        label_zh="实证研究",
        label_ja="実証研究",
        description_en=(
            "Material-grounded empirical paper. Currently in preview: "
            "full source-grounding is being added in a later release; "
            "until then, empirical claims may not be tied to verified "
            "primary sources. Use case analysis for material-bound "
            "work in the meantime if you need rigorous evidence "
            "anchoring today."
        ),
        description_zh=(
            "材料锚定的实证论文。当前为预览形态：完整的来源溯源能力将在后续"
            "版本完整启用；在此之前，实证论断可能未经一手材料核验。如果当前"
            "就需要严格的材料归位，建议先用「个案分析」模式。"
        ),
        description_ja=(
            "資料に根ざした実証論文。現在プレビュー段階："
            "完全な出典追跡機能は後続リリースで提供予定。"
            "それまで実証主張は一次資料への検証が完了していない可能性がある。"
            "厳密な証拠付けが今すぐ必要であれば、まず「個別事例分析」モードを推奨。"
        ),
        status="developer_preview",
        requires_capability=(CAPABILITY_EVIDENCE_LEDGER,),
        drafter_section_plan=_EMPIRICAL_SECTIONS,
        permits_empirical_chapters=True,
        primary_material_required=True,
    ),
    "theory_article": PaperModeSpec(
        mode_id="theory_article",
        label_en="Theoretical article",
        label_zh="理论论文",
        label_ja="理論論文",
        description_en=(
            "Pure conceptual / theoretical work whose contribution is "
            "the theoretical formulation itself. No empirical chapters. "
            "Examples may appear as illustrations only. Developer "
            "preview: requires the framework_lens phase (now available "
            "via PR-C2). Drafter still soaking on the theory_article "
            "section plan; opt in via the wizard preview confirmation."
        ),
        description_zh=(
            "纯概念 / 理论工作，论文的贡献本身就是理论建构。不含实证章节。"
            "案例仅作为说明使用。开发者预览：需要使用框架镜框节点（PR-C2 已上线）。"
            "草稿撰写阶段的理论章节计划尚在试运行；请通过向导预览确认进入。"
        ),
        description_ja=(
            "純概念・理論的考察を主軸とし、論文の貢献そのものが理論構築となる作業。"
            "実証章は含まない。事例は例示にとどまる。"
            "デベロッパープレビュー：フレームワーク・レンズフェーズ（PR-C2 で提供開始）が必要です。"
            "理論記事用の章構成は試運転中です。ウィザードのプレビュー確認を経て選択してください。"
        ),
        status="developer_preview",
        requires_capability=(CAPABILITY_FRAMEWORK_LENS,),
        drafter_section_plan=_THEORY_ARTICLE_SECTIONS,
        permits_empirical_chapters=False,
        primary_material_required=False,
    ),
    "comparative_study": PaperModeSpec(
        mode_id="comparative_study",
        label_en="Comparative study",
        label_zh="比较研究",
        label_ja="比較研究",
        description_en=(
            "Side-by-side comparison of two or more cases (regions, "
            "periods, traditions). Section plan includes parallel "
            "chapters per comparator. Coming soon."
        ),
        description_zh=(
            "对两个或多个对象（地区 / 时期 / 学派）的并列比较。"
            "章节方案包含每个比较项的对应章节。即将开放。"
        ),
        description_ja=(
            "二つ以上の対象（地域・時期・学派）を並列に比較する研究。"
            "章構成には各比較対象に対応する章が含まれる。近日公開。"
        ),
        status="coming_soon",
        requires_capability=(CAPABILITY_COMPARATIVE_SCAFFOLD,),
        drafter_section_plan=_EMPIRICAL_SECTIONS,  # placeholder
        permits_empirical_chapters=True,
        primary_material_required=False,
    ),
    "review_article": PaperModeSpec(
        mode_id="review_article",
        label_en="Review article",
        label_zh="综述论文",
        label_ja="総説論文",
        description_en=(
            "Survey of existing scholarship on a topic. No empirical "
            "chapters; deepened historiography. Coming soon."
        ),
        description_zh=("对某主题既有研究的综述。不含实证章节；强化学术史脉络。即将开放。"),
        description_ja=(
            "あるテーマに関する既存研究の総説。実証章は含まず、学術史の脈絡を深める。近日公開。"
        ),
        status="coming_soon",
        requires_capability=(CAPABILITY_REVIEW_ARTICLE,),
        drafter_section_plan=_REVIEW_ARTICLE_SECTIONS,
        permits_empirical_chapters=False,
        primary_material_required=False,
    ),
    "theory_review": PaperModeSpec(
        mode_id="theory_review",
        label_en="Theoretical review",
        label_zh="理论综述",
        label_ja="理論的レビュー",
        description_en=(
            "Review of theoretical literature in a problem space, "
            "tracing concept genealogy and school lineages rather "
            "than empirical findings. Different source weighting and "
            "review checks from a general review article. Coming soon."
        ),
        description_zh=(
            "针对某一问题域的理论文献综述，追溯概念谱系与学派源流，"
            "而非实证发现。来源加权与审查侧重与综述论文不同。即将开放。"
        ),
        description_ja=(
            "ある問題領域の理論文献レビュー。実証的発見ではなく概念系譜と学派の系譜を辿る。"
            "出典の重み付けや審査の重点が一般の総説論文と異なる。近日公開。"
        ),
        status="coming_soon",
        requires_capability=(CAPABILITY_THEORY_REVIEW,),
        drafter_section_plan=_THEORY_REVIEW_SECTIONS,
        permits_empirical_chapters=False,
        primary_material_required=False,
    ),
}


REGISTRY_VERSION = "v1"
DEFAULT_MODE_ID = "case_analysis"


def all_modes() -> list[PaperModeSpec]:
    """Return all registered modes, ordered as displayed in the UI."""
    # Display order: available first, then developer_preview, then
    # coming_soon. Within each tier, registry insertion order.
    by_status = {"available": 0, "developer_preview": 1, "coming_soon": 2}
    return sorted(_REGISTRY.values(), key=lambda spec: by_status[spec.status])


def get_mode(mode_id: str) -> PaperModeSpec | None:
    """Lookup by ``mode_id``. Returns ``None`` for unknown modes
    (caller decides whether to error or fall back)."""
    return _REGISTRY.get(mode_id)


def is_mode_id_known(mode_id: str) -> bool:
    """Cheap predicate for validators."""
    return mode_id in _REGISTRY


class ModeNotAvailableError(ValueError):
    """Raised when a caller tries to use a mode whose backing PR
    hasn't shipped (status=='coming_soon'), or when
    developer_preview is selected without explicit opt-in."""


def assert_mode_creatable(
    mode_id: str,
    *,
    accept_developer_preview: bool = False,
) -> PaperModeSpec:
    """Validate a mode for run creation / mutation.

    Returns the spec on success. Raises:
    - ``KeyError`` if ``mode_id`` isn't in the registry.
    - ``ModeNotAvailableError`` if the spec's status is
      ``coming_soon``, or if ``developer_preview`` is set and the
      caller didn't pass ``accept_developer_preview=True``.
    """
    spec = _REGISTRY.get(mode_id)
    if spec is None:
        raise KeyError(f"unknown paper_mode: {mode_id!r}")
    if spec.status == "coming_soon":
        raise ModeNotAvailableError(
            f"paper_mode {mode_id!r} is coming_soon; backing PR has not "
            f"shipped yet (requires_capability={spec.requires_capability})",
        )
    if spec.status == "developer_preview" and not accept_developer_preview:
        raise ModeNotAvailableError(
            f"paper_mode {mode_id!r} is in developer_preview; the user "
            "must explicitly acknowledge preview limitations before this "
            "mode can be selected",
        )
    return spec


def serialize_for_api() -> dict[str, object]:
    """Shape returned by ``GET /api/paper_modes``. Cached at app
    init by the frontend; backend re-evaluates only when the
    process restarts."""
    return {
        "registry_version": REGISTRY_VERSION,
        "default_mode_id": DEFAULT_MODE_ID,
        "modes": [
            {
                "mode_id": spec.mode_id,
                "label_en": spec.label_en,
                "label_zh": spec.label_zh,
                "label_ja": spec.label_ja,
                "description_en": spec.description_en,
                "description_zh": spec.description_zh,
                "description_ja": spec.description_ja,
                "status": spec.status,
                "requires_capability": list(spec.requires_capability),
                "permits_empirical_chapters": spec.permits_empirical_chapters,
                "primary_material_required": spec.primary_material_required,
            }
            for spec in all_modes()
        ],
    }
