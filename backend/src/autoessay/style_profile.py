"""Aggregate user style profile builder with prior-paper privacy boundaries."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.clients import pdf_text
from autoessay.config import get_settings
from autoessay.models import CorpusDocument, Project

# PR-B2 (codex AGREE-with-amendments to issue 2 of the 2026-05-01
# design review): all language-specific assets are now keyed by
# language code. ``detect_language`` resolves a text/document to a
# code; ``_lang_assets`` exposes the per-language tuple/sets.
HEDGING_PHRASES_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": (
        "may",
        "might",
        "could",
        "perhaps",
        "likely",
        "suggests",
        "appears",
        "seems",
        "tends to",
        "broadly",
        "roughly",
    ),
    # Conservative Chinese hedging set covering the common academic
    # softeners. Each phrase must be unambiguous on its own (no
    # single-character matches that would over-trigger).
    "zh": (
        "可能",
        "或许",
        "也许",
        "似乎",
        "倾向",
        "大致",
        "大体上",
        "据称",
        "估计",
        "应该",
    ),
    "ja": (
        "かもしれない",
        "可能性",
        "おそらく",
        "思われる",
        "傾向",
        "だろう",
        "ようだ",
        "推測",
    ),
}

STOP_WORDS_BY_LANG: dict[str, set[str]] = {
    "en": {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "with",
    },
    # Common Chinese function words that swamp TF-IDF when not
    # filtered. Conservative — only particles and the highest-
    # frequency function words. Domain terms (e.g. 经济, 历史) are
    # legitimate signal even if frequent within a corpus.
    "zh": {
        "的",
        "是",
        "在",
        "了",
        "和",
        "与",
        "及",
        "等",
        "我",
        "我们",
        "你",
        "他",
        "她",
        "它",
        "这",
        "那",
        "这是",
        "不",
        "不是",
        "也",
        "都",
        "又",
        "已",
        "已经",
        "而",
        "因",
        "为",
        "由于",
        "因为",
        "所以",
        "把",
        "被",
        "从",
        "以",
        "之",
        "其",
        "上",
        "下",
        "中",
        "中的",
        "或",
        "并",
        "但",
        "却",
        "对",
        "对于",
        "如",
        "如同",
    },
    "ja": {
        "の",
        "に",
        "は",
        "を",
        "が",
        "と",
        "で",
        "も",
        "から",
        "まで",
        "より",
        "へ",
        "や",
        "など",
        "として",
        "という",
        "そして",
        "また",
        "ある",
        "いる",
        "する",
        "した",
        "なる",
        "なった",
        "これ",
        "それ",
        "あれ",
        "ここ",
        "そこ",
        "あそこ",
    },
}

# Backward-compat aliases. Some external readers / older imports
# expect the bare HEDGING_PHRASES / STOP_WORDS names.
HEDGING_PHRASES: tuple[str, ...] = HEDGING_PHRASES_BY_LANG["en"]
STOP_WORDS: set[str] = STOP_WORDS_BY_LANG["en"]


# CJK code-point ranges used by ``detect_language``. We treat
# Hiragana / Katakana presence as a strong signal for Japanese
# (Chinese rarely contains these), and Han ideographs as Chinese
# absent any kana.
_HAN_RE = re.compile(r"[一-鿿]")
_HIRAGANA_RE = re.compile(r"[぀-ゟ]")
_KATAKANA_RE = re.compile(r"[゠-ヿ]")


def detect_language(text: str) -> str:
    """Heuristic language detection for style-profile purposes.

    Returns ``"zh"`` if the text is dominated by Han ideographs,
    ``"ja"`` if any Hiragana/Katakana is present (kana presence is
    diagnostic — Chinese rarely uses them), ``"en"`` otherwise.
    Empty / whitespace-only input falls back to ``"en"``.
    """
    if not text or not text.strip():
        return "en"
    han = len(_HAN_RE.findall(text))
    kana = len(_HIRAGANA_RE.findall(text)) + len(_KATAKANA_RE.findall(text))
    total = max(1, len(text))
    if kana > 0 and kana / total >= 0.005:
        return "ja"
    if han / total >= 0.05:
        return "zh"
    return "en"


class LengthDistribution(BaseModel):
    mean: float = 0.0
    p25: float = 0.0
    p75: float = 0.0


class StyleProfile(BaseModel):
    paragraph_length_distribution: LengthDistribution = Field(default_factory=LengthDistribution)
    sentence_length_distribution: LengthDistribution = Field(default_factory=LengthDistribution)
    opener_patterns: list[str] = Field(default_factory=list)
    hedging_patterns: list[str] = Field(default_factory=list)
    # ``taboo_phrases`` is intentionally empty here. Codex amendment 6
    # to issue 2 of the 2026-05-01 review: don't pretend the list is
    # *inferred* from the corpus until user-supplied taboo settings
    # exist. The field stays for downstream-consumer schema stability.
    taboo_phrases: list[str] = Field(default_factory=list)
    common_domain_terms: list[str] = Field(default_factory=list)
    short_local_examples: list[str] = Field(default_factory=list)
    # Diagnostics surface so the workspace UI (and any future
    # corpus-page surfacing) can directly answer the user's
    # "是不是假的？" question. Codex amendment 5 to issue 2.
    detected_language: str = "unknown"
    document_count: int = 0
    total_token_count: int = 0
    empty_section_warnings: list[str] = Field(default_factory=list)


def empty_style_profile() -> StyleProfile:
    return StyleProfile()


def build_style_profile(
    session: Session,
    project: Project,
    *,
    allow_prior_text: bool | None = None,
) -> StyleProfile:
    allow_examples = (
        get_settings().allow_prior_text if allow_prior_text is None else allow_prior_text
    )
    # Effective corpora = project-scoped + project-selected globals
    # (PR-B1, codex amendments 1+2 to issue 2 of the 2026-05-01
    # design review). ``corpora_for_project`` enforces owner
    # integrity and the explicit selection model (legacy projects
    # had every global pre-selected via migration 016 backfill).
    from autoessay.corpus import corpora_for_project

    corpora = corpora_for_project(session, project)
    if not corpora:
        return build_style_profile_from_paths([], allow_prior_text=allow_examples)
    corpus_ids = [corpus.id for corpus in corpora]
    documents = list(
        session.scalars(
            select(CorpusDocument).where(
                CorpusDocument.corpus_id.in_(corpus_ids),
                CorpusDocument.document_type == "prior_paper",
            ),
        ),
    )
    paths = [Path(document.extracted_text_path or document.source_path) for document in documents]
    return build_style_profile_from_paths(paths, allow_prior_text=allow_examples)


def build_style_profile_from_paths(
    paths: list[Path],
    *,
    allow_prior_text: bool,
) -> StyleProfile:
    texts = [_read_document_text(path) for path in paths]
    return build_style_profile_from_texts(texts, allow_prior_text=allow_prior_text)


def build_style_profile_from_texts(
    texts: list[str],
    *,
    allow_prior_text: bool,
) -> StyleProfile:
    document_count = len(texts)
    texts = [text for text in texts if text.strip()]
    if not texts:
        empty_warnings = (
            ["no documents provided"]
            if document_count == 0
            else ["all documents were empty after stripping"]
        )
        return StyleProfile(
            detected_language="unknown",
            document_count=document_count,
            total_token_count=0,
            empty_section_warnings=empty_warnings,
        )

    # Detect language on the concatenation; the per-document
    # variability is small in practice (a corpus is usually
    # mono-lingual), and the fallback paths are cheap when the
    # heuristic is wrong.
    language = detect_language("\n\n".join(texts))
    paragraphs = [paragraph for text in texts for paragraph in _paragraphs(text)]
    sentences = [sentence for text in texts for sentence in _sentences(text, language)]
    paragraph_lengths = [_word_count(paragraph, language) for paragraph in paragraphs]
    sentence_lengths = [_word_count(sentence, language) for sentence in sentences]
    snippets = _short_examples(paragraphs) if allow_prior_text else []
    openers = _top_openers(paragraphs, language)
    hedges = _top_hedges(texts, language)
    domain_terms = _top_tfidf_terms(texts, language)
    total_tokens = sum(paragraph_lengths)

    warnings: list[str] = []
    if not openers:
        warnings.append("opener_patterns: empty (no paragraphs after splitting)")
    if not hedges:
        warnings.append("hedging_patterns: no language-keyed hedges matched")
    if not domain_terms:
        warnings.append(
            "common_domain_terms: TF-IDF produced no terms — "
            "input may be too short or all-stopwords",
        )

    return StyleProfile(
        paragraph_length_distribution=_distribution(paragraph_lengths),
        sentence_length_distribution=_distribution(sentence_lengths),
        opener_patterns=openers,
        hedging_patterns=hedges,
        taboo_phrases=[],
        common_domain_terms=domain_terms,
        short_local_examples=snippets,
        detected_language=language,
        document_count=document_count,
        total_token_count=total_tokens,
        empty_section_warnings=warnings,
    )


def style_profile_summary(profile: StyleProfile) -> dict[str, object]:
    return profile.dict()


def _read_document_text(path: Path) -> str:
    if not path.exists():
        return ""
    if path.suffix.casefold() == ".pdf":
        try:
            return pdf_text.extract_text(path.read_bytes(), source_id=path.name)
        except Exception:  # noqa: BLE001 - fake PDFs in tests and poor extraction fall back to text decode.
            return _decode_bytes(path.read_bytes())
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _decode_bytes(path.read_bytes())


def _decode_bytes(value: bytes) -> str:
    return value.decode("utf-8", errors="ignore")


def _paragraphs(text: str) -> list[str]:
    return [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]


def _sentences(text: str, language: str = "en") -> list[str]:
    """Sentence splitter with full-width punctuation support for
    Chinese/Japanese."""
    if language in ("zh", "ja"):
        # Split on either Latin or full-width terminators. Whitespace
        # after the terminator is optional in CJK text.
        parts = re.split(r"(?<=[。！？!?.])\s*", text)
    else:
        parts = re.split(r"(?<=[.!?])\s+", text)
    return [sentence.strip() for sentence in parts if sentence.strip()]


def _distribution(values: list[int]) -> LengthDistribution:
    if not values:
        return LengthDistribution()
    ordered = sorted(values)
    return LengthDistribution(
        mean=sum(values) / len(values),
        p25=float(_percentile(ordered, 0.25)),
        p75=float(_percentile(ordered, 0.75)),
    )


def _percentile(ordered: list[int], fraction: float) -> int:
    if not ordered:
        return 0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def _top_openers(paragraphs: list[str], language: str = "en") -> list[str]:
    counter: Counter[str] = Counter()
    # Take more tokens for CJK because individual segmented tokens
    # are shorter; English's "first 4 tokens" gives ~20-30
    # characters, but CJK 4 tokens may be a fragment.
    take = 6 if language in ("zh", "ja") else 4
    join = "" if language in ("zh", "ja") else " "
    for paragraph in paragraphs:
        words = _tokens(paragraph, language)[:take]
        if words:
            counter[join.join(words)] += 1
    return [phrase for phrase, _count in counter.most_common(10)]


def _top_hedges(texts: list[str], language: str = "en") -> list[str]:
    """Match hedging phrases against the per-language list. For
    English we keep the word-boundary regex; for CJK languages
    where ``\\b`` doesn't fire on Han / Kana, we fall back to
    plain substring search since the hedging tokens we ship are
    multi-character and unambiguous."""
    counter: Counter[str] = Counter()
    phrases = HEDGING_PHRASES_BY_LANG.get(language, HEDGING_PHRASES_BY_LANG["en"])
    for text in texts:
        if language == "en":
            lowered = text.casefold()
            for phrase in phrases:
                matches = re.findall(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered)
                if matches:
                    counter[phrase] += len(matches)
        else:
            for phrase in phrases:
                count = text.count(phrase)
                if count:
                    counter[phrase] += count
    return [phrase for phrase, _count in counter.most_common(10)]


def _top_tfidf_terms(texts: list[str], language: str = "en") -> list[str]:
    term_counts = [Counter(_content_tokens(text, language)) for text in texts]
    if not term_counts:
        return []
    doc_frequency: Counter[str] = Counter()
    for counts in term_counts:
        for term in counts:
            doc_frequency[term] += 1
    scores: dict[str, float] = {}
    total_docs = len(term_counts)
    for counts in term_counts:
        for term, count in counts.items():
            idf = math.log((1 + total_docs) / (1 + doc_frequency[term])) + 1
            scores[term] = scores.get(term, 0.0) + count * idf
    return [
        term for term, _score in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:20]
    ]


def _short_examples(paragraphs: list[str]) -> list[str]:
    examples: list[str] = []
    for paragraph in paragraphs:
        normalized = re.sub(r"\s+", " ", paragraph).strip()
        if not normalized:
            continue
        examples.append(normalized[:200])
        if len(examples) >= 5:
            break
    return examples


def _content_tokens(text: str, language: str = "en") -> list[str]:
    stop_words = STOP_WORDS_BY_LANG.get(language, STOP_WORDS_BY_LANG["en"])
    # English content-token filter requires len > 3 to drop tiny
    # fillers like "and"/"the" the stop-list misses. CJK token
    # length is segmenter-dependent; drop only single-char tokens
    # (largely particles + interjections) and the explicit stop
    # list.
    if language == "en":
        return [
            token for token in _tokens(text, language) if len(token) > 3 and token not in stop_words
        ]
    return [
        token for token in _tokens(text, language) if len(token) >= 2 and token not in stop_words
    ]


def _tokens(text: str, language: str = "en") -> list[str]:
    """Tokenize ``text`` for the given language.

    English uses the existing ASCII-letters regex (and lowercases).
    Chinese uses ``jieba.cut`` when available; if jieba is missing
    or fails, falls back to character bigrams so the profile still
    returns SOME signal rather than an empty list. Japanese uses
    bigrams directly — jieba is Chinese-specific.
    """
    if language == "zh":
        return _zh_tokens(text)
    if language == "ja":
        return _cjk_bigrams(text)
    return re.findall(r"\b[a-z][a-z'-]*\b", text.casefold())


def _zh_tokens(text: str) -> list[str]:
    try:
        import jieba

        tokens = [
            token.strip()
            for token in jieba.cut(text, cut_all=False, HMM=True)
            if token.strip() and not _is_punctuation_or_whitespace(token)
        ]
        if tokens:
            return tokens
    except Exception:  # noqa: BLE001 — jieba can warn-then-fail on first import
        pass
    return _cjk_bigrams(text)


def _cjk_bigrams(text: str) -> list[str]:
    """Char-bigrams over CJK code points only, skipping ASCII /
    punctuation / whitespace. Used as a defensive fallback when
    jieba is unavailable, and as the primary tokenizer for
    Japanese (jieba targets Chinese)."""
    cjk_only = "".join(ch for ch in text if _is_cjk(ch))
    return [cjk_only[i : i + 2] for i in range(len(cjk_only) - 1)]


def _is_cjk(ch: str) -> bool:
    return bool(_HAN_RE.match(ch) or _HIRAGANA_RE.match(ch) or _KATAKANA_RE.match(ch))


def _is_punctuation_or_whitespace(token: str) -> bool:
    return all(not (ch.isalnum() or _is_cjk(ch)) for ch in token)


def _word_count(value: str, language: str = "en") -> int:
    """Word count metric. For CJK languages each Han / kana
    character counts as one ``word`` plus Latin word tokens via
    the regex; matches the user's mental model of paragraph length
    in Chinese papers (where character count, not space-delimited
    word count, is the relevant unit)."""
    if language in ("zh", "ja"):
        cjk = sum(1 for ch in value if _is_cjk(ch))
        latin = len(re.findall(r"\b[a-z][a-z'-]*\b", value.casefold()))
        return cjk + latin
    return len(re.findall(r"\b[\w'-]+\b", value))
