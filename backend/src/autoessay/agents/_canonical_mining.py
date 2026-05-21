"""PR-J9 v1: scout-side canonical / frontier literature mining.

Pipeline (per scout run, after query expansion, before/parallel-with
``_collect_sources``):

  1. ``mine_canonical_sources_via_llm`` — LLM call 1 (canon mode)
     returns ``CanonicalSourcesOutput`` (5 consensus + 3 disagreement
     each with 1-2 representative_works).
  2. ``mine_frontier_sources_via_llm`` — LLM call 2 (frontier mode)
     returns ``FrontierSourcesOutput`` (5 hot directions, each with
     1-2 representative_works).
  3. ``verify_canonical_via_crossref`` — for each LLM-named work,
     query Crossref (DOI exact when present, else title+author
     fuzzy ≥0.90); drop unverified items + audit ``hallucinated_canonical``.
  4. ``merge_canonical_with_search`` — dedup verified canon vs the
     vendor search results by source_id; canon wins on tie (preserve
     ``canonical_bucket`` + ``canonical_rationale``); tag everything
     with ``provenance``.

Codex round-1 amendments folded:
  * 2 — ``CanonicalArticle`` renamed to ``CanonicalWork``; canon
        includes monographs / book chapters.
  * 3.1 — option B: 2 separate LLM calls (canon vs frontier) so the
        reasoning frames don't compete for attention.
  * 3.2 — verify threshold ≥0.90 fuzzy on title; first-author family
        match required when no DOI.
  * 3.3 — ``provenance`` + ``canonical_bucket`` + ``canonical_rationale``
        added to ``NormalizedSource`` in clients/common.py;
        ``source_client`` keeps real verifier (crossref / openalex),
        not "llm_canon".
  * 3.5 — caller serializes (no shared-session gather) for v1.
  * 4 — opaque kernel; ``getattr`` access + degrade-on-missing.
  * 5.4 — scope is research subject/period, not publication-date filter.
  * 8 — system prompt explicitly says canon is content not instruction.

PR-J9b follow-up (NOT in v1): standalone LLM rerank with
``frontier_currency`` weight + 4-axis scoring. v1 piggybacks on
PR-J8's curator kernel-aware ranking — canonical sources land in the
candidate pool with ``provenance="llm_canon"`` + ``canonical_bucket``,
and curator's existing ranking scores them on the user's kernel.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence

from autoessay.agents._canonical_sources_schema import (
    CanonicalBucket,
    CanonicalSourcesOutput,
    CanonicalWork,
    FrontierSourcesOutput,
    iter_canonical_works,
    iter_frontier_works,
)
from autoessay.agents._language import language_directive
from autoessay.agents._research_kernel_prompt import (
    KERNEL_INJECTION_GUARD,
    research_kernel_for_prompt,
)
from autoessay.clients.common import NormalizedSource
from autoessay.clients.crossref import CrossrefClient
from autoessay.clients.openalex import OpenAlexClient
from autoessay.config import get_settings
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

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def is_stub_enabled() -> bool:
    """When True the mining + verify chain is short-circuited and
    ``mine_and_verify_canonical_sources`` returns an empty list. CI /
    pytest fixtures set this so canon mining never hits a real LLM /
    Crossref endpoint."""
    return bool(get_settings().canonical_mining_stub)


async def mine_canonical_sources_via_llm(
    *,
    run: Run,
    project: Project,
    session: object,  # SQLAlchemy session — duck-typed to avoid import cycle
    hooks: HookRegistry,
    title: str,
    research_kernel: Mapping[str, object] | None,
    domain_id: str | None,
) -> CanonicalSourcesOutput:
    """LLM call 1 (canon mode) — consensus + disagreement.

    Returns the parsed :class:`CanonicalSourcesOutput`. Raises
    :class:`SchemaViolationError` after 2 corrective retries; caller
    catches and surfaces as a warning (does NOT fail the scout phase
    — canon mining is enrichment, not gating).
    """
    kernel_payload = research_kernel_for_prompt(research_kernel)
    user_payload = {
        "title": title,
        "research_kernel": kernel_payload,
        "domain_id": domain_id,
    }
    user_prompt = (
        "Identify the established canonical scholarly literature for the "
        "user's topic. Return one strict JSON object with TWO arrays:\n"
        "  1. consensus_findings: ≤5 items. Each = a major consensus "
        "statement of top scholars in this field, with 1-2 "
        "representative_works (most-cited / earliest / most-influential).\n"
        "  2. major_disagreements: ≤3 items. Each = a major scholarly "
        "disagreement axis, with 1 representative_work from each side "
        "(2 works total per axis).\n\n"
        "Each work object MUST include: title (4-400 chars), first_author, "
        "year (if known), doi (if known), journal_or_publisher (if known), "
        "rationale (≤400 chars; why this work is canon).\n\n"
        "Constraints (CHECK BEFORE RETURNING):\n"
        "- Only include works you are HIGHLY CONFIDENT exist (your "
        "training-time knowledge); we will verify each via Crossref.\n"
        "- Works should match the kernel's research subject + period; "
        "modern reviews of historical periods are OK when authoritative.\n"
        "- Prefer enduring canon over recent flashes (frontier comes in "
        "the next call); for humanities canon, monographs / book "
        "chapters are AS valid as journal articles.\n"
        "- Do NOT invent DOIs. If unsure, omit the doi field.\n"
        "- If the topic's canon is sparse (narrow / nascent field), "
        "return fewer items rather than fabricate.\n\n"
        f"Inputs: {json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}"
    )
    system_prompt = (
        "You are CanonicalSourcesScout (canon mode). Identify the "
        "established scholarly canon — the works top scholars in this "
        "field cite as foundational and the major disagreement axes "
        "between schools of thought. Anchor on the user's title and "
        "research_kernel. "
        + KERNEL_INJECTION_GUARD
        + " Return one strict JSON object. "
        + language_directive(project.language or "en")
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=2200,
        response_format={"type": "json_object"},
        request_id="scout_canonical_mining",
        prompt_template_id="scout.canonical_mining.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="discovery",
        step_id="scout.canonical_mining",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=title,
        run_metadata={
            "agent_phase": "scout",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "mining_mode": "canon",
        },
    )
    audit = AuditWriter(
        session=session,  # type: ignore[arg-type]
        run_dir=run.run_dir,
        agent_name="ScoutCanonical",
    )
    response = await run_llm_step(
        request=request,
        hooks=hooks,
        context=context,
        output_schema=CanonicalSourcesOutput,
        audit=audit,
        max_corrective_retries=2,
        llm_optional=False,
    )
    parsed = response.parsed
    if isinstance(parsed, CanonicalSourcesOutput):
        return parsed
    if isinstance(parsed, Mapping):
        return CanonicalSourcesOutput.parse_obj(dict(parsed))
    raise SchemaViolationError(
        "canonical mining returned non-Mapping payload",
        attempts=[response],
    )


async def mine_frontier_sources_via_llm(
    *,
    run: Run,
    project: Project,
    session: object,
    hooks: HookRegistry,
    title: str,
    research_kernel: Mapping[str, object] | None,
    domain_id: str | None,
) -> FrontierSourcesOutput:
    """LLM call 2 (frontier mode) — current hot directions."""
    kernel_payload = research_kernel_for_prompt(research_kernel)
    user_payload = {
        "title": title,
        "research_kernel": kernel_payload,
        "domain_id": domain_id,
    }
    user_prompt = (
        "Identify the most ACTIVE current scholarly directions / "
        "debates in this field as of your training cutoff. Return one "
        "strict JSON object:\n"
        "  frontier_hotspots: ≤5 items. Each = a current direction or "
        "new viewpoint that has gained traction in the past ~5-10 years, "
        "with 1-2 representative_works (most influential RECENT work in "
        "that direction, NOT older canon) and a why_frontier "
        "(10-400 chars) rationale.\n\n"
        "Each work object MUST include: title, first_author, year "
        "(prefer recent), doi (if known), journal_or_publisher, "
        "rationale (≤400 chars).\n\n"
        "Constraints (CHECK BEFORE RETURNING):\n"
        "- Recent ≠ trivial. A frontier item must have explanatory or "
        "methodological novelty, not just a new dataset of an old "
        "question.\n"
        "- If the topic's frontier is dormant (some classical "
        "humanities subfields), return fewer items rather than "
        "fabricate.\n"
        "- Do NOT invent DOIs. If unsure, omit the doi field.\n"
        "- Old canon belongs in the previous call (canon mode); this "
        "call is for the past ~5-10 years.\n\n"
        f"Inputs: {json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}"
    )
    system_prompt = (
        "You are CanonicalSourcesScout (frontier mode). Identify the "
        "active scholarly frontier — recent directions, methods, "
        "viewpoints that have gained traction in the past ~5-10 years. "
        "NOT older canon (that is the previous call). Anchor on the "
        "user's title and research_kernel. "
        + KERNEL_INJECTION_GUARD
        + " Return one strict JSON object. "
        + language_directive(project.language or "en")
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.3,  # slightly higher for frontier breadth
        max_tokens=1800,
        response_format={"type": "json_object"},
        request_id="scout_frontier_mining",
        prompt_template_id="scout.frontier_mining.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="discovery",
        step_id="scout.frontier_mining",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=title,
        run_metadata={
            "agent_phase": "scout",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "mining_mode": "frontier",
        },
    )
    audit = AuditWriter(
        session=session,  # type: ignore[arg-type]
        run_dir=run.run_dir,
        agent_name="ScoutFrontier",
    )
    response = await run_llm_step(
        request=request,
        hooks=hooks,
        context=context,
        output_schema=FrontierSourcesOutput,
        audit=audit,
        max_corrective_retries=2,
        llm_optional=False,
    )
    parsed = response.parsed
    if isinstance(parsed, FrontierSourcesOutput):
        return parsed
    if isinstance(parsed, Mapping):
        return FrontierSourcesOutput.parse_obj(dict(parsed))
    raise SchemaViolationError(
        "frontier mining returned non-Mapping payload",
        attempts=[response],
    )


# ----------------------------------------------------------------------
# Crossref verification
# ----------------------------------------------------------------------


# PR-J9b: signature kept for back-compat — internally now delegates
# to ``verify_canonical_dual_source`` so the Crossref-only path still
# works for existing tests; new code should call the dual-source helper
# directly.
async def verify_canonical_via_crossref(
    works: Sequence[tuple[CanonicalBucket, str, CanonicalWork]],
    *,
    crossref_client: CrossrefClient | None = None,
    fuzzy_threshold: float = 0.90,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """Crossref-only verification (PR-J9 v1 entry point). Returns
    ``(verified_sources, drop_warnings)``. PR-J9b adds OpenAlex
    fallback via ``verify_canonical_dual_source``; this helper keeps
    Crossref-only semantics for backward-compat tests + paths that
    deliberately want Crossref-only behavior."""
    return await verify_canonical_dual_source(
        works,
        crossref_client=crossref_client,
        openalex_client=None,
        fuzzy_threshold=fuzzy_threshold,
    )


async def verify_canonical_dual_source(
    works: Sequence[tuple[CanonicalBucket, str, CanonicalWork]],
    *,
    crossref_client: CrossrefClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    fuzzy_threshold: float = 0.90,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """PR-J9b: dual-source verify. Try Crossref first (DOI exact short-
    circuits, else normalized title fuzzy + author family + year ±2);
    when Crossref drops the work, fall back to OpenAlex with the same
    composite threshold. ``openalex_client`` SHOULD be created with
    ``filters=None`` (codex round-1 A5 — the default OpenAlex filter
    ``publication_year:>2018,is_oa:true,type:article`` excludes Amsden
    1989, Wade 1990, Cumings 1981, and other monograph canon).

    Each verified source carries the J9 provenance fields plus the
    new J9b ``verified_by`` field (``"crossref"`` or ``"openalex"``).
    Crossref-only mode (``openalex_client=None``) preserves J9 v1
    semantics — drops a work with a single ``below_fuzzy_threshold``
    warning instead of escalating.
    """
    cr_client = crossref_client or CrossrefClient()
    verified: list[NormalizedSource] = []
    warnings: list[dict[str, object]] = []
    for bucket, rationale, work in works:
        crossref_result = await _verify_via_crossref(
            cr_client, bucket, rationale, work, fuzzy_threshold=fuzzy_threshold
        )
        if isinstance(crossref_result, NormalizedSource):
            if openalex_client is not None and _source_needs_text_enrichment(crossref_result):
                openalex_result = await _verify_via_openalex(
                    openalex_client,
                    bucket,
                    rationale,
                    work,
                    fuzzy_threshold=fuzzy_threshold,
                )
                if isinstance(openalex_result, NormalizedSource):
                    verified.append(_merge_verified_metadata(crossref_result, openalex_result))
                    continue
            verified.append(crossref_result)
            continue
        if openalex_client is None:
            warnings.append(crossref_result)
            continue
        openalex_result = await _verify_via_openalex(
            openalex_client, bucket, rationale, work, fuzzy_threshold=fuzzy_threshold
        )
        if isinstance(openalex_result, NormalizedSource):
            verified.append(openalex_result)
            continue
        warnings.append(_combine_drop_warnings(crossref_result, openalex_result))
    return verified, warnings


async def _verify_via_crossref(
    client: CrossrefClient,
    bucket: CanonicalBucket,
    rationale: str,
    work: CanonicalWork,
    *,
    fuzzy_threshold: float,
) -> NormalizedSource | dict[str, object]:
    """Crossref leg of the dual-verify. Returns NormalizedSource on
    accept (with ``verified_by="crossref"``), drop-warning dict
    otherwise."""
    return await _verify_one_work(
        verifier_name="crossref",
        search=lambda q: client.search(query=q, year_window=None, limit=5),
        bucket=bucket,
        rationale=rationale,
        work=work,
        fuzzy_threshold=fuzzy_threshold,
    )


async def _verify_via_openalex(
    client: OpenAlexClient,
    bucket: CanonicalBucket,
    rationale: str,
    work: CanonicalWork,
    *,
    fuzzy_threshold: float,
) -> NormalizedSource | dict[str, object]:
    """OpenAlex leg of the dual-verify. Returns NormalizedSource on
    accept (with ``verified_by="openalex"``), drop-warning dict
    otherwise. Caller SHOULD pass an OpenAlexClient instantiated with
    ``filters=None`` so monograph canon is reachable (codex round-1
    A5)."""
    return await _verify_one_work(
        verifier_name="openalex",
        search=lambda q: client.search(query=q, year_window=None, limit=5),
        bucket=bucket,
        rationale=rationale,
        work=work,
        fuzzy_threshold=fuzzy_threshold,
    )


async def _verify_one_work(
    *,
    verifier_name: str,
    search: Callable[[str], Awaitable[list[NormalizedSource]]],
    bucket: CanonicalBucket,
    rationale: str,
    work: CanonicalWork,
    fuzzy_threshold: float,
) -> NormalizedSource | dict[str, object]:
    """Single-work verification, parametric on the search backend.
    Returns NormalizedSource on accept, drop-warning dict on reject.

    PR-J9b: title fuzzy uses normalized-token comparison rather than
    raw ``difflib.SequenceMatcher`` — that lets a monograph with a
    long subtitle (``"Asia's Next Giant: South Korea and Late
    Industrialization"`` vs the OpenAlex listing
    ``"Asia's Next Giant"``) clear ≥0.90 fuzzy without manual subtitle
    stripping (codex round-1 A5)."""
    query_parts = [work.title, work.first_author]
    if work.year is not None:
        query_parts.append(str(work.year))
    query = " ".join(query_parts)
    try:
        candidates = await search(query)
    except Exception:  # noqa: BLE001 — verification failure is per-work
        return {
            "work_title": work.title,
            "first_author": work.first_author,
            "year": work.year,
            "reason": f"{verifier_name}_query_failed",
            "best_match_score": 0.0,
        }
    if not candidates:
        return {
            "work_title": work.title,
            "first_author": work.first_author,
            "year": work.year,
            "reason": f"no_{verifier_name}_match",
            "best_match_score": 0.0,
        }
    best_score = 0.0
    best_candidate: NormalizedSource | None = None
    work_doi_norm = (work.doi or "").lower().strip()
    work_first_family = _family_name(work.first_author).casefold()
    for candidate in candidates:
        cand_doi = (candidate.doi or "").lower().strip()
        if work_doi_norm and cand_doi and work_doi_norm == cand_doi:
            best_candidate = candidate
            best_score = 1.0
            break
        title_score = _normalized_title_fuzzy(work.title, candidate.title)
        family_match = any(
            work_first_family and work_first_family in _family_name(author).casefold()
            for author in candidate.authors
        )
        # PR-J9b: year proximity widened to ±2 for monographs (reprints +
        # OpenAlex/Crossref publication-year off-by-one are common for
        # older books, codex round-1 A5).
        year_match = (
            work.year is not None
            and candidate.year is not None
            and abs(work.year - candidate.year) <= 2
        )
        score = title_score * 0.7 + (0.2 if family_match else 0.0) + (0.1 if year_match else 0.0)
        if score > best_score:
            best_score = score
            best_candidate = candidate
    if best_candidate is None or best_score < fuzzy_threshold:
        return {
            "work_title": work.title,
            "first_author": work.first_author,
            "year": work.year,
            "reason": f"{verifier_name}_below_fuzzy_threshold",
            "best_match_score": round(best_score, 3),
        }
    return NormalizedSource(
        source_id=best_candidate.source_id,
        title=best_candidate.title,
        authors=list(best_candidate.authors),
        year=best_candidate.year,
        venue=best_candidate.venue,
        doi=best_candidate.doi,
        url=best_candidate.url,
        pdf_url=best_candidate.pdf_url,
        abstract=best_candidate.abstract,
        source_client=best_candidate.source_client,
        access_status=best_candidate.access_status,
        license=best_candidate.license,
        rank_score=best_candidate.rank_score,
        risk_flags=list(best_candidate.risk_flags),
        research_role=best_candidate.research_role,
        provenance="llm_canon",
        canonical_bucket=bucket,
        canonical_rationale=(work.rationale or rationale)[:200],
        verified_by=verifier_name,
    )


def _source_needs_text_enrichment(source: NormalizedSource) -> bool:
    return not source.abstract and not source.pdf_url


def _merge_verified_metadata(
    primary: NormalizedSource,
    enrichment: NormalizedSource,
) -> NormalizedSource:
    """Keep the primary verifier's identity while borrowing richer text metadata.

    Crossref often verifies older canonical works by DOI but does not expose
    abstracts. OpenAlex may expose the same DOI/title with an inverted-index
    abstract. This is still the same verified work; preserving the Crossref
    ``source_id`` avoids churn while giving synthesizer usable source text.
    """
    update: dict[str, object] = {}
    if enrichment.abstract and (
        not primary.abstract or len(enrichment.abstract) > len(primary.abstract)
    ):
        update["abstract"] = enrichment.abstract
    for field in ("pdf_url", "license", "url", "venue", "year"):
        current = getattr(primary, field)
        candidate = getattr(enrichment, field)
        if not current and candidate:
            update[field] = candidate
    if enrichment.pdf_url and not primary.pdf_url:
        update["access_status"] = enrichment.access_status
    return primary.copy(update=update) if update else primary


def _coerce_float(value: object) -> float:
    """Drop-warning dicts type their values as ``object`` (Mapping[str, object]
    in caller-facing API). For numeric coercion we narrow safely without
    relying on ``object`` having arithmetic structure."""
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _combine_drop_warnings(
    crossref_drop: dict[str, object],
    openalex_drop: dict[str, object],
) -> dict[str, object]:
    """When both verifiers drop a work, surface a combined warning so
    the run audit shows we tried both legs."""
    return {
        "work_title": crossref_drop.get("work_title"),
        "first_author": crossref_drop.get("first_author"),
        "year": crossref_drop.get("year"),
        "reason": "dual_verify_below_threshold",
        "crossref_reason": crossref_drop.get("reason"),
        "crossref_score": crossref_drop.get("best_match_score", 0.0),
        "openalex_reason": openalex_drop.get("reason"),
        "openalex_score": openalex_drop.get("best_match_score", 0.0),
        "best_match_score": max(
            _coerce_float(crossref_drop.get("best_match_score")),
            _coerce_float(openalex_drop.get("best_match_score")),
        ),
    }


# Punctuation + hyphen + colon stripped; tokens lowered + de-duped on
# whitespace. Used by ``_normalized_title_fuzzy``.
_TITLE_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def _normalize_title_for_fuzzy(title: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. PR-J9b A5
    fix: long-subtitle monographs (``"Title: Subtitle of N words"``)
    no longer fail fuzzy because of the colon-and-subtitle suffix —
    we keep tokens but treat them as a bag-of-tokens via Sequence
    matcher on the normalized form."""
    if not title:
        return ""
    lowered = title.casefold()
    no_punct = _TITLE_PUNCT_RE.sub(" ", lowered)
    return " ".join(no_punct.split())


def _normalized_title_fuzzy(left: str, right: str) -> float:
    """Composite of (a) SequenceMatcher on the normalized full title
    and (b) max of SequenceMatcher on each side's pre-colon prefix.
    Picks the larger so a monograph with a subtitle (``"Asia's Next
    Giant: South Korea and Late Industrialization"``) still matches
    a source listing only ``"Asia's Next Giant"``. Codex round-1 A5."""
    if not left or not right:
        return 0.0
    left_norm = _normalize_title_for_fuzzy(left)
    right_norm = _normalize_title_for_fuzzy(right)
    full_score = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    left_prefix = _normalize_title_for_fuzzy(left.split(":", 1)[0])
    right_prefix = _normalize_title_for_fuzzy(right.split(":", 1)[0])
    prefix_score = (
        difflib.SequenceMatcher(None, left_prefix, right_prefix).ratio()
        if left_prefix and right_prefix
        else 0.0
    )
    return max(full_score, prefix_score)


def _family_name(full_name: str) -> str:
    """Best-effort family name extraction. Western name: last token.
    East-Asian (single token / no space): the whole token.
    """
    cleaned = " ".join(full_name.split())
    if not cleaned:
        return ""
    parts = cleaned.split(" ")
    return parts[-1] if len(parts) > 1 else cleaned


# ----------------------------------------------------------------------
# Merge with vendor search results
# ----------------------------------------------------------------------


def merge_canonical_with_search(
    canonical: Sequence[NormalizedSource],
    search: Sequence[NormalizedSource],
) -> list[NormalizedSource]:
    """Dedup canonical (verified, provenance=llm_canon) with vendor
    search results by ``source_id``. Canonical wins on tie — keeps the
    canon's ``canonical_bucket`` + ``canonical_rationale`` while
    inheriting the vendor's other metadata.

    Order: canonical entries first (so they're visible in the
    skim_candidates listing), then vendor entries that didn't get
    deduped against a canonical entry.
    """
    canonical_by_id: dict[str, NormalizedSource] = {}
    for source in canonical:
        canonical_by_id[source.source_id] = source
    out: list[NormalizedSource] = list(canonical_by_id.values())
    for source in search:
        if source.source_id in canonical_by_id:
            continue
        # Vendor source — explicitly set provenance="search" so the
        # downstream UI / Curator can distinguish.
        if source.provenance == "search":
            out.append(source)
        else:
            out.append(
                source.copy(update={"provenance": "search"}) if hasattr(source, "copy") else source
            )
    return out


# ----------------------------------------------------------------------
# Top-level: mine + verify + merge (used by scout._run_scout_with_session)
# ----------------------------------------------------------------------


async def mine_and_verify_canonical_sources(
    *,
    run: Run,
    project: Project,
    session: object,
    hooks: HookRegistry,
    title: str,
    research_kernel: Mapping[str, object] | None,
    domain_id: str | None,
    crossref_client: CrossrefClient | None = None,
    openalex_client: OpenAlexClient | None = None,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """Top-level mining + verification helper. Returns
    ``(verified_sources, warnings)``. Caller (scout) merges verified
    sources into the dedup pool BEFORE writing skim_candidates.

    v1 serializes the two mining calls + Crossref verification (codex
    amendment 3.5: don't gather() with shared SQLAlchemy session;
    LLM audit writes use ``session``). Total wall ~10-15s for 16
    candidate works (5 consensus×1 + 3 disagreement×2 + 5 frontier×1).

    On stub mode (``Settings.canonical_mining_stub=True``) returns
    empty list immediately — caller continues with vendor-only flow.
    """
    if is_stub_enabled():
        return [], []
    warnings: list[dict[str, object]] = []
    canonical_pairs: list[tuple[CanonicalBucket, str, CanonicalWork]] = []
    try:
        canon = await mine_canonical_sources_via_llm(
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            title=title,
            research_kernel=research_kernel,
            domain_id=domain_id,
        )
        canonical_pairs.extend(iter_canonical_works(canon))
    except SchemaViolationError as exc:
        warnings.append(
            {
                "source_id": "llm_canon",
                "failure_class": "fixable_prompt",
                "message": f"canon mining schema violation; skipped: {exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001 — mining is enrichment, not gating
        warnings.append(
            {
                "source_id": "llm_canon",
                "failure_class": "fixable_prompt",
                "message": f"canon mining transport failure; skipped: {exc}",
            }
        )
    try:
        frontier = await mine_frontier_sources_via_llm(
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            title=title,
            research_kernel=research_kernel,
            domain_id=domain_id,
        )
        canonical_pairs.extend(iter_frontier_works(frontier))
    except SchemaViolationError as exc:
        warnings.append(
            {
                "source_id": "llm_frontier",
                "failure_class": "fixable_prompt",
                "message": f"frontier mining schema violation; skipped: {exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001 — same enrichment-not-gating
        warnings.append(
            {
                "source_id": "llm_frontier",
                "failure_class": "fixable_prompt",
                "message": f"frontier mining transport failure; skipped: {exc}",
            }
        )
    if not canonical_pairs:
        return [], warnings
    # PR-J9b: dual-source verify. The OpenAlex client MUST use
    # ``filters=None`` — the default filter
    # ``publication_year:>2018,is_oa:true,type:article`` would exclude
    # exactly the monograph canon (Amsden 1989, Wade 1990, Cumings
    # 1981) the OpenAlex fallback exists to recover (codex round-1 A5).
    if openalex_client is None:
        openalex_client_owned = OpenAlexClient(filters=None)
        openalex_for_verify: OpenAlexClient | None = openalex_client_owned
    else:
        openalex_client_owned = None
        openalex_for_verify = openalex_client
    try:
        verified, drop_warnings = await verify_canonical_dual_source(
            canonical_pairs,
            crossref_client=crossref_client,
            openalex_client=openalex_for_verify,
        )
    finally:
        if openalex_client_owned is not None:
            await openalex_client_owned.aclose()
    for drop in drop_warnings:
        drop_title = str(drop.get("work_title", ""))[:80]
        crossref_reason = drop.get("crossref_reason") or drop.get("reason")
        openalex_reason = drop.get("openalex_reason")
        if openalex_reason:
            message = (
                f"canonical work could not be verified via crossref or openalex: "
                f"crossref={crossref_reason}/{drop.get('crossref_score')} "
                f"openalex={openalex_reason}/{drop.get('openalex_score')}"
            )
        else:
            message = (
                f"canonical work could not be verified via crossref: "
                f"reason={crossref_reason} score={drop.get('best_match_score')}"
            )
        warnings.append(
            {
                "source_id": f"llm_canon:{drop_title}",
                "failure_class": "fixable_deterministic",
                "message": message,
            }
        )
    return verified, warnings


__all__ = [
    "is_stub_enabled",
    "merge_canonical_with_search",
    "mine_and_verify_canonical_sources",
    "mine_canonical_sources_via_llm",
    "mine_frontier_sources_via_llm",
    "verify_canonical_dual_source",
    "verify_canonical_via_crossref",
]
