import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseSettings, Field, root_validator

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXPRESS_ARS_SKILL_PATH = (
    BACKEND_ROOT / "data" / "ars" / "academic-research-skills" / "academic-paper" / "SKILL.md"
)


@dataclass(frozen=True)
class LLMProviderSpec:
    """One row in the multi-provider fallback chain.

    ``base_url`` is the OpenAI-compatible server root that hosts
    ``/v1/chat/completions``. ``model`` is the per-provider real
    model name to send in the request body (which may differ across
    providers — e.g. ``gpt-5.4-mini`` on rightcode/apiport vs
    ``MiniMax-M2.7`` on minimax). ``api_key`` is the bearer token
    for that specific provider; tokens are not shared.
    """

    name: str
    base_url: str
    api_key: str
    model: str


class Settings(BaseSettings):
    autoessay_env: str = Field("local", env="AUTOESSAY_ENV")
    base_url: str = Field("http://localhost:3017", env="AUTOESSAY_BASE_URL")
    data_dir: Path = Field(Path("./data"), env="AUTOESSAY_DATA_DIR")
    database_url: str = Field("sqlite:///./autoessay.sqlite3", env="DATABASE_URL")
    redis_url: str = Field("redis://localhost:6379/0", env="REDIS_URL")
    one_api_base_url: str = Field("http://localhost:3000", env="ONE_API_BASE_URL")
    one_api_token: str = Field("dev-one-api-token-placeholder", env="ONE_API_TOKEN")
    one_api_model: str = Field("gpt-4o-mini", env="ONE_API_MODEL")
    llm_request_timeout_seconds: float = Field(
        180.0,
        env="AUTOESSAY_LLM_REQUEST_TIMEOUT_SECONDS",
        ge=30.0,
    )
    # Multi-provider fallback chain (JSON list). When set, LLMClient
    # iterates these in order, advancing to the next provider on
    # transient failures (5xx / 429 / connect / timeout / per-provider
    # 401-403). Each item: {"name", "base_url", "api_key", "model"}.
    # When empty, a single-provider chain is synthesized from the
    # legacy ONE_API_* fields above so existing prod env keeps working.
    llm_providers_raw: str = Field("", env="AUTOESSAY_LLM_PROVIDERS")
    appleseed_memory_base_url: str = Field(
        "http://localhost:8010",
        env=["APPLESEED_MEMORY_URL", "APPLESEED_MEMORY_BASE_URL"],
    )
    appleseed_memory_token: str = Field(
        "",
        env="APPLESEED_MEMORY_TOKEN",
    )
    appleseed_orchestrator_base_url: str | None = Field(
        None,
        env="APPLESEED_ORCHESTRATOR_BASE_URL",
    )
    appleseed_orchestrator_token: str | None = Field(
        None,
        env="APPLESEED_ORCHESTRATOR_TOKEN",
    )
    appleseed_roundtable_base_url: str | None = Field(
        None,
        env="APPLESEED_ROUNDTABLE_BASE_URL",
    )
    appleseed_roundtable_token: str | None = Field(
        None,
        env="APPLESEED_ROUNDTABLE_TOKEN",
    )
    originality_api_key: str | None = Field(None, env="ORIGINALITY_API_KEY")
    originality_base_url: str = Field(
        "https://api.originality.ai",
        env="ORIGINALITY_BASE_URL",
    )
    gptzero_api_key: str | None = Field(None, env="GPTZERO_API_KEY")
    gptzero_base_url: str = Field("https://api.gptzero.me", env="GPTZERO_BASE_URL")
    copyleaks_email: str | None = Field(None, env="COPYLEAKS_EMAIL")
    copyleaks_api_key: str | None = Field(None, env="COPYLEAKS_API_KEY")
    copyleaks_auth_base_url: str = Field(
        "https://id.copyleaks.com",
        env="COPYLEAKS_AUTH_BASE_URL",
    )
    copyleaks_base_url: str = Field("https://api.copyleaks.com", env="COPYLEAKS_BASE_URL")
    critic_stub: bool = Field(False, env="AUTOESSAY_CRITIC_STUB")
    integrity_stub: bool = Field(False, env="AUTOESSAY_INTEGRITY_STUB")
    safety_gate_stub: bool = Field(False, env="AUTOESSAY_SAFETY_GATE_STUB")
    # Slice H follow-up: safety classifier needs a more capable model than
    # the default gpt-5.4-mini, which over-allowed obvious off-topic titles
    # like recipe / spam in canary tests. gpt-5.5 is the strictest tier
    # available on the configured proxy chain.
    safety_gate_model: str = Field("gpt-5.5", env="SAFETY_GATE_LLM_MODEL")
    safety_gate_enabled: bool = Field(True, env="AUTOESSAY_SAFETY_GATE_ENABLED")
    # Slice H: prod fails closed when the LLM classifier is unavailable.
    # Set this in e2e/dev/canary or emergency degradation paths to preserve
    # the previous fail-open behavior.
    safety_gate_fail_open: bool = Field(False, env="AUTOESSAY_SAFETY_GATE_FAIL_OPEN")
    kernel_suggest_stub: bool = Field(False, env="AUTOESSAY_KERNEL_SUGGEST_STUB")
    kernel_suggest_model: str = Field("gpt-5.4", env="AUTOESSAY_KERNEL_SUGGEST_MODEL")
    kernel_suggest_max_tokens: int = Field(
        900,
        env="AUTOESSAY_KERNEL_SUGGEST_MAX_TOKENS",
        ge=200,
        le=3000,
    )
    session_secret: str = Field(
        "dev-session-secret-placeholder",
        env=["AUTOESSAY_SESSION_SECRET", "SESSION_SECRET"],
    )
    initial_admin_username: str | None = Field(None, env="AUTOESSAY_INITIAL_ADMIN_USERNAME")
    initial_admin_password_hash: str | None = Field(
        None,
        env="AUTOESSAY_INITIAL_ADMIN_PASSWORD_HASH",
    )
    initial_admin_display_name: str = Field(
        "Administrator",
        env="AUTOESSAY_INITIAL_ADMIN_DISPLAY_NAME",
    )
    auth_bypass: bool = Field(False, env="AUTOESSAY_AUTH_BYPASS")
    log_level: str = Field("INFO", env="LOG_LEVEL")
    git_sha: str = Field("unknown", env="GIT_SHA")
    image_tag: str = Field("local", env="AUTOESSAY_IMAGE_TAG")
    rq_queue_name: str = Field("autoessay", env="AUTOESSAY_RQ_QUEUE")
    domain_dir: Path = Field(Path("./domains"), env="AUTOESSAY_DOMAIN_DIR")
    sync_worker: bool = Field(False, env="AUTOESSAY_SYNC_WORKER")
    manuscript_default_mode: Literal["express", "deep"] = Field(
        "express",
        env="MANUSCRIPT_DEFAULT_MODE",
    )
    express_token_cap: int = Field(100000, env="AUTOESSAY_EXPRESS_TOKEN_CAP", ge=1000)
    express_timeout_seconds: int = Field(
        300,
        env="AUTOESSAY_EXPRESS_TIMEOUT_SECONDS",
        ge=30,
    )
    express_manuscript_max_tokens: int = Field(
        60000,
        env="AUTOESSAY_EXPRESS_MANUSCRIPT_MAX_TOKENS",
        ge=1000,
    )
    express_audit_max_tokens: int = Field(
        8000,
        env="AUTOESSAY_EXPRESS_AUDIT_MAX_TOKENS",
        ge=1000,
    )
    express_model: str = Field(
        "gpt-5.4",
        env=["AUTOESSAY_EXPRESS_MODEL", "AUTOESSAY_EXPRESS_CODEX_MODEL"],
    )
    express_codex_model: str = Field("gpt-5.4", env="AUTOESSAY_EXPRESS_CODEX_MODEL")
    express_ars_skill_path: Path = Field(
        DEFAULT_EXPRESS_ARS_SKILL_PATH,
        env="AUTOESSAY_EXPRESS_ARS_SKILL_PATH",
    )
    proposal_stub: bool = Field(False, env="AUTOESSAY_PROPOSAL_STUB")
    scout_stub: bool = Field(False, env="AUTOESSAY_SCOUT_STUB")
    # PR-J6: scout post-LLM title-overlap gate ratio. The hook rejects
    # query-expansion responses where fewer than this fraction of the
    # generated queries share at least one keyword with the title or
    # ``research_kernel.tentative_question``. Default 0.5 (codex round-1
    # AGREE-with-amendments — bounded 0.25..1.0; override via
    # ``AUTOESSAY_SCOUT_TITLE_ANCHOR_RATIO``).
    scout_title_anchor_ratio: float = Field(
        0.5,
        env="AUTOESSAY_SCOUT_TITLE_ANCHOR_RATIO",
        ge=0.25,
        le=1.0,
    )
    # PR-J9: scout LLM canonical/frontier mining (separate from
    # scout_stub which already covers vendor lit clients). When True,
    # the mining + Crossref-verify chain is short-circuited; the
    # scout shortlist is the vendor-only set with ``provenance="search"``
    # everywhere (matches PR-J6 / PR-J8 behavior). CI / e2e / pytest
    # set this True so canon mining never hits a real LLM gateway.
    canonical_mining_stub: bool = Field(False, env="AUTOESSAY_CANONICAL_MINING_STUB")
    memory_read: bool = Field(False, env="AUTOESSAY_MEMORY_READ")
    curator_stub: bool = Field(False, env="AUTOESSAY_CURATOR_STUB")
    # PR-J9b: when True, curator skips the 4-axis LLM rerank prompt and
    # falls back to the legacy single-axis ``relevance``-only path
    # (preserves all existing curator e2e/pytest determinism). When
    # False (prod default), curator asks LLM for scope_fit / relevance
    # / impact / frontier_currency and uses 0.85*rerank + 0.15*legacy
    # as the final rank. Distinct from ``curator_stub`` — that one
    # short-circuits ALL curator LLM; this one only the 4-axis layer.
    curator_rerank_stub: bool = Field(False, env="AUTOESSAY_CURATOR_RERANK_STUB")
    pdf_fetch_browser_fallback: bool = Field(
        True,
        env="AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK",
    )
    # Slice G (#17.1.2 user ask): verification gate - default verified-only.
    # Set to True to include UNVERIFIED/DISPUTED/PENDING sources in citation
    # pool (experimental; warns at runtime).
    include_unverified_in_citation_pool: bool = Field(
        False,
        env="AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL",
    )
    # PR-I2.a: proactive zombie-phase reaper. Reuses PR-I1's
    # ``_recover_zombie_running_phase`` semantics (lock-age + last-phase-
    # event idle + no terminal event) but runs unconditionally on a
    # uvicorn lifespan task instead of waiting for the user to click
    # ``start_*`` again. PR-I3: default flipped to True so new
    # deployments get self-healing without ops needing to remember an
    # env flag. Override with ``AUTOESSAY_ZOMBIE_REAPER_ENABLED=0`` to
    # disable. The reaper does NOT lower the idle threshold (still
    # ``AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS`` = 15 min) — it's just a
    # push-driven trigger for the same gate. Single-uvicorn deployments
    # are safe with default True; multi-replica deployments need a
    # DB-level reaper-lease row first (see ``zombie_reaper.py:16``).
    zombie_reaper_enabled: bool = Field(True, env="AUTOESSAY_ZOMBIE_REAPER_ENABLED")
    zombie_reaper_interval_seconds: int = Field(
        300,
        env="AUTOESSAY_ZOMBIE_REAPER_INTERVAL_SECONDS",
        ge=30,
    )
    # PR-C3.a: tension_extraction phase gates (codex round-2 amendment 6
    # — operational gate, NOT the ``CAPABILITY_TENSION_TAXONOMY``
    # paper_modes constant misused as boolean). When False (default
    # until C3.b lands lens / drafter / frontend integration), the
    # phase is skipped via ``should_run_tension_extraction`` and the
    # new ``TENSION_EXTRACTION_RUNNING`` / ``USER_TENSION_REVIEW``
    # states stay dormant. ``tension_extraction_stub`` mirrors
    # framework_lens / canonical_mining / curator_rerank: when True,
    # the LLM call is short-circuited to deterministic stub output;
    # CI / e2e / pytest set this so tension never hits a real LLM
    # gateway.
    tension_taxonomy_enabled: bool = Field(False, env="AUTOESSAY_TENSION_TAXONOMY_ENABLED")
    tension_extraction_stub: bool = Field(False, env="AUTOESSAY_TENSION_EXTRACTION_STUB")
    synthesizer_stub: bool = Field(False, env="AUTOESSAY_SYNTHESIZER_STUB")
    synthesizer_min_processed_sources: int = Field(
        3,
        env="AUTOESSAY_SYNTHESIZER_MIN_PROCESSED_SOURCES",
    )
    front_matter_stub: bool = Field(False, env="AUTOESSAY_FRONT_MATTER_STUB")
    self_check_stub: bool = Field(False, env="AUTOESSAY_SELF_CHECK_STUB")
    material_diagnostic_stub: bool = Field(False, env="AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB")
    detailed_outline_stub: bool = Field(False, env="AUTOESSAY_DETAILED_OUTLINE_STUB")
    ideator_stub: bool = Field(False, env="AUTOESSAY_IDEATOR_STUB")
    # PR-C2c: framework_lens phase has both a deterministic stub path and
    # a real LLM enrichment path (see backend/src/autoessay/agents/
    # framework_lens.py). Default False so prod hits the LLM enrichment;
    # CI / e2e / tests set this to True (frontend/scripts/run-e2e-server.sh
    # and pytest fixtures) so the phase stays deterministic and free.
    framework_lens_stub: bool = Field(False, env="AUTOESSAY_FRAMEWORK_LENS_STUB")
    drafter_stub: bool = Field(False, env="AUTOESSAY_DRAFTER_STUB")
    # Per-section schema-validation retries before falling back to a
    # stub. Stage 3.E follow-up: bumped 2→4 because a partial-stub draft
    # is now treated as ``phase_done`` (not FAILED_FIXABLE), so each
    # extra retry directly reduces the number of stubbed sections the
    # user has to review later.
    drafter_max_corrective_retries: int = Field(
        4,
        env="AUTOESSAY_DRAFTER_MAX_CORRECTIVE_RETRIES",
    )
    stylist_stub: bool = Field(False, env="AUTOESSAY_STYLIST_STUB")
    # Final rewrite is now part of the production path by default. The
    # phase includes bounded polish-loop defenses, critic-loop scoring, and
    # RQ timeout protection; operators can still disable it for emergency
    # rollback with AUTOESSAY_FINAL_REWRITE_ENABLED=0.
    final_rewrite_enabled: bool = Field(True, env="AUTOESSAY_FINAL_REWRITE_ENABLED")
    final_rewrite_stub: bool = Field(False, env="AUTOESSAY_FINAL_REWRITE_STUB")
    critic_loop_enabled: bool = Field(True, env="AUTOESSAY_CRITIC_LOOP_ENABLED")
    critic_loop_iterations: int = Field(
        3,
        env="AUTOESSAY_CRITIC_LOOP_ITERATIONS",
        ge=0,
        le=5,
    )
    # Round-0 holistic revision: insert a single unconditional "整体性改稿"
    # round before each loop's structured rounds, so the LLM integrates the
    # critic's revision items into one rewrite before per-item repair runs.
    # Default OFF + canary per codex AGREE-WITH-AMENDMENTS 2026-05-12:
    # north-star v3 gate is at -1.0 / 0.0 / 0.0 (bretton boundary), no margin
    # for an unvalidated extra rewrite turn. Operators flip these to "1" for
    # canary real-paper validation; a follow-up PR flips defaults after data
    # supports it.
    polish_holistic_round0_enabled: bool = Field(
        False,
        env="AUTOESSAY_POLISH_HOLISTIC_ROUND0",
    )
    critic_loop_holistic_round0_enabled: bool = Field(
        False,
        env="AUTOESSAY_CRITIC_LOOP_HOLISTIC_ROUND0",
    )
    # Effective context-window estimate for round-0 preflight. Default 100000
    # is conservative for one-api channels backed by gpt-4o / gpt-4o-mini
    # (128k window, with 28k reserved for safety + max_output). Operators
    # tighten this when the channel maps to a smaller-context provider.
    round0_context_window_tokens: int = Field(
        100000,
        env="AUTOESSAY_ROUND0_CONTEXT_WINDOW_TOKENS",
        ge=8000,
    )
    north_star_gate_enabled: bool = Field(True, env="AUTOESSAY_NORTH_STAR_GATE_ENABLED")
    north_star_gate_force_samples: int = Field(
        0,
        env="AUTOESSAY_NORTH_STAR_GATE_FORCE_SAMPLES",
        ge=0,
        le=5,
    )
    exports_policy_max_polish_retries: int = Field(
        2,
        env="AUTOESSAY_EXPORTS_POLICY_MAX_POLISH_RETRIES",
        ge=0,
        le=5,
    )
    # v3 holistic mode: default ON. This keeps final_rewrite as a
    # full-manuscript seam smoother while preserving citations,
    # paragraph structure, evidence relationships, and claim_map.
    final_rewrite_holistic: bool = Field(True, env="AUTOESSAY_FINAL_REWRITE_HOLISTIC")
    # v3 phase-context accumulation: default ON. Later phase LLM calls
    # receive a budgeted, non-citable context pack built from upstream
    # artifacts so argument framing and material limits survive phase
    # boundaries. This does not loosen approved-source / claim-map /
    # citation gates.
    phase_context_accumulation: bool = Field(
        True,
        env="AUTOESSAY_PHASE_CONTEXT_ACCUMULATION",
    )
    phase_context_budget_chars: int = Field(
        12000,
        env="AUTOESSAY_PHASE_CONTEXT_BUDGET_CHARS",
        ge=3000,
    )
    # PR-262 + Slice F: shadow-baseline runner stub. Default OFF so
    # production runs generate the real model-backed shadow baseline.
    # Tests / e2e set AUTOESSAY_SHADOW_BASELINE_STUB=1 explicitly so
    # they stay deterministic and do not burn LLM budget.
    shadow_baseline_stub: bool = Field(False, env="AUTOESSAY_SHADOW_BASELINE_STUB")
    # TEST-only acceptance switch. Default OFF so the shadow baseline
    # stays an independent benchmark. When explicitly enabled, the
    # persisted shadow-baseline manuscript is exposed downstream as a
    # synthetic approved source for polish/reasoning validation.
    baseline_as_evidence_test: bool = Field(
        False,
        env="AUTOESSAY_BASELINE_AS_EVIDENCE_TEST",
    )
    # PR-G-Sources Stage 1 (codex round-2 amendment Q1): synthesizer
    # deep-dive limit. ``None`` means "use the per-domain
    # ``search.telescope.deep_dive_limit`` then DEFAULT_DEEP_DIVE_LIMIT
    # (14)". Set the env var (positive int) to force every domain to
    # the same value (e.g. for a single real-paper acceptance walk).
    synthesizer_deep_dive_limit: int | None = Field(
        None,
        env="AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT",
    )
    # PR-G-Sources Stage 2 (codex round-2 amendment Q2): drafter
    # cited-sources diversity floor. drafter post-LLM checks
    # ``cited_count >= effective_floor = min(this, eligible_source_count)``;
    # below floor triggers a single bounded diversity-repair retry on
    # the 2-3 lowest-density sections. Codex round-2: 12 is the
    # midpoint between v0.3.1 empirical (4-7) and shadow_baseline 14.
    cited_sources_diversity_floor: int = Field(
        12,
        env="AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR",
    )
    # PR-G-Sources Q2 (codex v5 round-1 amendment): drafter LLM
    # diversity repair. When ``cited_count < effective_floor`` and
    # there are unused eligible sources, drafter runs one bounded
    # LLM call to rewrite the 2 lowest-density sections so they
    # integrate the unused sources. Default True; ops can disable
    # via env to short-circuit the extra LLM call (~$0.05).
    diversity_repair_enabled: bool = Field(
        True,
        env="AUTOESSAY_DIVERSITY_REPAIR_ENABLED",
    )
    # PR-G-CriticScores wire-up (codex round-3 AGREE on v4
    # polish-loop design): operational gate for the critic polish
    # loop. Skeleton landed False in #274; wire-up flips this to
    # True default so prod runs surface ``reviews/polish_quality.json``
    # for the acceptance gate. ``critic_stub=True`` short-circuits
    # the LLM call regardless (CI determinism); when shadow_baseline
    # is missing or stub-mode, status = ``skipped_no_real_baseline``.
    polish_loop_enabled: bool = Field(
        True,
        env="AUTOESSAY_POLISH_LOOP_ENABLED",
    )
    # PR-G-CriticScores R5 (codex round-2 amendment): the polish loop
    # adds 5 LLM calls (1 blind eval + 2 rewrite + 2 re-eval) on top
    # of the existing critic LLM call. The legacy 600s RQ timeout is
    # too tight; raise to 1200s so a worst-case run with
    # anti-plagiarism retries doesn't hit the timeout.
    rq_timeout_critic_seconds: int = Field(
        1200,
        env="AUTOESSAY_RQ_TIMEOUT_CRITIC",
        ge=300,
    )
    # PR-G-Coherence (codex round-3 AGREE on v4 polish-loop design):
    # drafter post-section global coherence pass. After all 8 sections
    # are written + CNKI wrapper applied but before disk persist, run
    # one extra LLM pass over the assembled manuscript to tighten
    # cross-section transitions / 首尾呼应 / 删重复 / 补转折 — without
    # changing [N] citations, CNKI structure, or 摘要/关键词/参考文献
    # blocks. Default True; set ``=0`` to short-circuit (e.g. for
    # cost-bounded acceptance walks).
    drafter_global_coherence_enabled: bool = Field(
        True,
        env="AUTOESSAY_DRAFTER_GLOBAL_COHERENCE_ENABLED",
    )
    # PR-G-CiteMarkerGate PR-2b (2026-05-07): when the deterministic
    # cite-marker normalize leaves citation-shaped markers behind
    # (form ``(Author Year)`` / ``[Author Year]`` / unresolvable
    # ``[crossref:DOI]`` / ``[https://...]`` not in cited_sources),
    # run a focused LLM repair on the affected paragraphs. Codex
    # AGREE-WITH-AMENDMENTS direction B: max retries default 2,
    # exhaustion → ``failed_policy``. Default OFF until validated
    # in a real-paper round; flip via env to validate progressively.
    drafter_cite_marker_repair_enabled: bool = Field(
        False,
        env="AUTOESSAY_DRAFTER_CITE_MARKER_REPAIR_ENABLED",
    )
    drafter_cite_marker_max_retries: int = Field(
        2,
        env="AUTOESSAY_DRAFTER_CITE_MARKER_MAX_RETRIES",
    )
    # Slice D (#5 verify-by-source toggle): per-phase + per-claim-type policy.
    # - "strict": claim must have shortlist source citation
    # - "soft": claim_map marks evidence_status=model_backed/source_bound + confidence;
    #   prose stays clean
    # - "off": no requirement (only used for prototyping; not prod-default)
    verify_by_source_drafting_source_bound: Literal["strict", "soft", "off"] = Field(
        "strict",
        env="AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_SOURCE_BOUND",
    )
    verify_by_source_drafting_analytic: Literal["strict", "soft", "off"] = Field(
        "soft",
        env="AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_ANALYTIC",
    )
    verify_by_source_final: Literal["strict", "soft", "off"] = Field(
        "strict",
        env="AUTOESSAY_VERIFY_BY_SOURCE_FINAL",
    )
    # Slice D (#6 evidence whitelist toggle): per-phase only (always
    # applies only to conclusion section).
    evidence_whitelist_drafting: Literal["strict", "soft", "off"] = Field(
        "soft",
        env="AUTOESSAY_EVIDENCE_WHITELIST_DRAFTING",
    )
    evidence_whitelist_final: Literal["strict", "soft", "off"] = Field(
        "strict",
        env="AUTOESSAY_EVIDENCE_WHITELIST_FINAL",
    )
    stop_slop_dir: Path = Field(Path("/app/stop_slop"), env="AUTOESSAY_STOP_SLOP_DIR")
    stop_slop_llm_enabled: bool = Field(True, env="AUTOESSAY_STOP_SLOP_LLM_ENABLED")
    allow_prior_text: bool = Field(False, env="AUTOESSAY_ALLOW_PRIOR_TEXT")
    max_upload_mb: int = Field(30, env="AUTOESSAY_MAX_UPLOAD_MB")
    semantic_scholar_api_key: str | None = Field(None, env="SEMANTIC_SCHOLAR_API_KEY")
    openalex_api_key: str | None = Field(None, env="OPENALEX_API_KEY")
    openalex_mailto: str | None = Field(None, env="OPENALEX_MAILTO")
    openalex_stub: bool = Field(False, env="AUTOESSAY_OPENALEX_STUB")
    crossref_mailto: str | None = Field(None, env="CROSSREF_MAILTO")
    cnki_api_base_url: str = Field(
        "http://localhost.com:6001/api/search",
        env="CNKI_API_BASE_URL",
    )
    cnki_stub: bool = Field(False, env="AUTOESSAY_CNKI_STUB")
    local_dedup_stub: bool = Field(False, env="AUTOESSAY_LOCAL_DEDUP_STUB")
    # PR-248 test-mode flag (codex Q3 verdict from PR-245 design):
    # gates the test-only ``POST /api/test/runs/{id}/fail-phase``
    # endpoint that injects a deterministic FAILED_FIXABLE so
    # Playwright retry-leg specs don't have to wait for a real
    # agent crash. Hard-rejected in production env via the same
    # root_validator pattern as auth_bypass.
    test_mode: bool = Field(False, env="AUTOESSAY_TEST_MODE")

    @root_validator
    def _reject_production_auth_bypass(cls, values: dict[str, object]) -> dict[str, object]:
        autoessay_env = str(values.get("autoessay_env", "")).lower()
        auth_bypass = bool(values.get("auth_bypass", False))
        if autoessay_env == "production" and auth_bypass:
            raise ValueError("AUTOESSAY_AUTH_BYPASS=1 is not allowed when AUTOESSAY_ENV=production")
        test_mode = bool(values.get("test_mode", False))
        if autoessay_env == "production" and test_mode:
            raise ValueError("AUTOESSAY_TEST_MODE=1 is not allowed when AUTOESSAY_ENV=production")
        return values

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def get_llm_providers() -> list[LLMProviderSpec]:
    """Resolve the LLM provider fallback chain from settings.

    If ``AUTOESSAY_LLM_PROVIDERS`` is set, parse it as a non-empty
    JSON list of {name, base_url, api_key, model} objects (order =
    fallback priority, first tried first). Otherwise synthesize a
    one-element chain from the legacy ``ONE_API_*`` fields so
    callers that haven't been migrated keep working unchanged.
    """
    settings = get_settings()
    raw = (settings.llm_providers_raw or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"AUTOESSAY_LLM_PROVIDERS is not valid JSON: {exc}",
            ) from exc
        if not isinstance(data, list) or not data:
            raise ValueError("AUTOESSAY_LLM_PROVIDERS must be a non-empty JSON list")
        providers: list[LLMProviderSpec] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"AUTOESSAY_LLM_PROVIDERS[{index}] must be a JSON object",
                )
            for key in ("name", "base_url", "api_key", "model"):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"AUTOESSAY_LLM_PROVIDERS[{index}].{key} must be a non-empty string",
                    )
            providers.append(
                LLMProviderSpec(
                    name=item["name"].strip(),
                    base_url=item["base_url"].strip(),
                    api_key=item["api_key"].strip(),
                    model=item["model"].strip(),
                ),
            )
        return providers
    return [
        LLMProviderSpec(
            name="default",
            base_url=settings.one_api_base_url,
            api_key=settings.one_api_token,
            model=settings.one_api_model,
        ),
    ]
