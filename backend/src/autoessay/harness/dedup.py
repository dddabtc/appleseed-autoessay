"""Local multilingual paragraph deduplication for Drafter outputs."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from autoessay.clients import pdf_text
from autoessay.clients.common import NormalizedSource
from autoessay.config import get_settings
from autoessay.harness.types import HookContext, HookResult, LLMCallResponse
from autoessay.models import Corpus, CorpusDocument, Project

MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
SIMILARITY_THRESHOLD = 0.85
MIN_PARAGRAPH_CHARS = 40
MAX_MATCHES_PER_PARAGRAPH = 5


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalDedupUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TextRecord:
    record_id: str
    kind: str
    title: str
    text: str
    start: int | None = None
    end: int | None = None
    source_id: str | None = None
    document_id: str | None = None
    paragraph_index: int = 0


@dataclass(frozen=True)
class ParagraphSpan:
    text: str
    start: int
    end: int


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = MODEL_NAME) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise LocalDedupUnavailable(
                "sentence-transformers is not installed; install backend[ml] to enable local dedup",
            ) from exc
        self._model = SentenceTransformer(model_name)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self._model.encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]


class DrafterLocalDedupHook:
    def __init__(
        self,
        *,
        run_dir: Path,
        user_id: str | None,
        session: Session,
        shortlist: Sequence[NormalizedSource],
        project: Project | None = None,
    ) -> None:
        self._run_dir = run_dir
        self._user_id = user_id
        self._project = project
        self._session = session
        self._shortlist = list(shortlist)
        self._post_llm_sections = 0

    def post_llm(self, _ctx: HookContext, response: LLMCallResponse) -> HookResult:
        if response.validation_result.valid:
            self._post_llm_sections += 1
        return HookResult(annotations={"sections_seen": self._post_llm_sections})

    def write_final(self, manuscript: str) -> dict[str, object]:
        return run_local_dedup(
            run_id=self._run_dir.name,
            run_dir=self._run_dir,
            user_id=self._user_id,
            project=self._project,
            session=self._session,
            manuscript=manuscript,
            shortlist=self._shortlist,
        )


def run_local_dedup(
    *,
    run_id: str,
    run_dir: Path,
    user_id: str | None,
    session: Session | None,
    manuscript: str,
    shortlist: Sequence[NormalizedSource],
    project: Project | None = None,
    embedder: Embedder | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> dict[str, object]:
    integrity_dir = run_dir / "integrity"
    integrity_dir.mkdir(parents=True, exist_ok=True)
    output_path = integrity_dir / "local_dedup.json"

    draft_records = _draft_paragraphs(manuscript)
    comparison_records = _comparison_paragraphs(
        run_dir=run_dir,
        user_id=user_id,
        project=project,
        session=session,
        shortlist=shortlist,
    )

    if get_settings().local_dedup_stub:
        payload = _payload(
            run_id=run_id,
            status="stubbed",
            draft_records=draft_records,
            comparison_records=comparison_records,
            matches=[],
            threshold=threshold,
            reason="AUTOESSAY_LOCAL_DEDUP_STUB=1",
        )
        _write_json(output_path, payload)
        return payload

    if not draft_records or not comparison_records:
        payload = _payload(
            run_id=run_id,
            status="ok",
            draft_records=draft_records,
            comparison_records=comparison_records,
            matches=[],
            threshold=threshold,
            reason=None,
        )
        _write_json(output_path, payload)
        return payload

    try:
        active_embedder = embedder or SentenceTransformerEmbedder()
        draft_vectors = active_embedder.embed([record.text for record in draft_records])
        comparison_vectors = active_embedder.embed([record.text for record in comparison_records])
    except Exception as exc:  # noqa: BLE001 - local dedup must not block Drafter.
        payload = _payload(
            run_id=run_id,
            status="skipped",
            draft_records=draft_records,
            comparison_records=comparison_records,
            matches=[],
            threshold=threshold,
            reason=str(exc),
        )
        _write_json(output_path, payload)
        return payload

    try:
        store = SQLiteVectorStore(integrity_dir / "local_dedup.sqlite3")
        store.replace_records(comparison_records, comparison_vectors)
        matches = _find_matches(
            draft_records=draft_records,
            draft_vectors=draft_vectors,
            comparison_records=store.records(),
            threshold=threshold,
        )
    except Exception as exc:  # noqa: BLE001 - local dedup must not block Drafter.
        payload = _payload(
            run_id=run_id,
            status="skipped",
            draft_records=draft_records,
            comparison_records=comparison_records,
            matches=[],
            threshold=threshold,
            reason=f"local dedup storage failed: {exc}",
        )
        _write_json(output_path, payload)
        return payload
    payload = _payload(
        run_id=run_id,
        status="ok",
        draft_records=draft_records,
        comparison_records=comparison_records,
        matches=matches,
        threshold=threshold,
        reason=None,
    )
    _write_json(output_path, payload)
    return payload


class SQLiteVectorStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: list[tuple[TextRecord, list[float]]] = []

    def replace_records(
        self, records: Sequence[TextRecord], vectors: Sequence[list[float]]
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        with sqlite3.connect(self._path) as connection:
            sqlite_vec_loaded = _try_load_sqlite_vec(connection)
            connection.execute(
                """
                CREATE TABLE comparison_vectors (
                    rowid INTEGER PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    vector_json TEXT NOT NULL
                )
                """,
            )
            dimension = len(vectors[0]) if vectors else 0
            if sqlite_vec_loaded and dimension > 0:
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE vec_comparison "
                        f"USING vec0(embedding float[{dimension}])",
                    )
                except sqlite3.Error:
                    sqlite_vec_loaded = False
            for index, (record, vector) in enumerate(zip(records, vectors, strict=True), start=1):
                connection.execute(
                    "INSERT INTO comparison_vectors(rowid, record_json, vector_json) "
                    "VALUES (?, ?, ?)",
                    (index, json.dumps(_record_payload(record)), json.dumps(vector)),
                )
                if sqlite_vec_loaded:
                    _insert_sqlite_vec(connection, index, vector)
            connection.commit()
        self._records = list(zip(records, vectors, strict=True))

    def records(self) -> list[tuple[TextRecord, list[float]]]:
        return list(self._records)


def _try_load_sqlite_vec(connection: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        return True
    except (AttributeError, sqlite3.Error):
        with suppress(sqlite3.Error):
            connection.enable_load_extension(False)
        return False


def _insert_sqlite_vec(
    connection: sqlite3.Connection,
    rowid: int,
    vector: Sequence[float],
) -> None:
    try:
        import sqlite_vec

        serialized = sqlite_vec.serialize_float32(vector)
        connection.execute(
            "INSERT INTO vec_comparison(rowid, embedding) VALUES (?, ?)",
            (rowid, serialized),
        )
    except (ImportError, AttributeError, sqlite3.Error):
        return


def _find_matches(
    *,
    draft_records: Sequence[TextRecord],
    draft_vectors: Sequence[list[float]],
    comparison_records: Sequence[tuple[TextRecord, list[float]]],
    threshold: float,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    match_index = 1
    for draft, draft_vector in zip(draft_records, draft_vectors, strict=True):
        scored: list[tuple[float, TextRecord]] = []
        for comparison, comparison_vector in comparison_records:
            score = _cosine(draft_vector, comparison_vector)
            if score > threshold:
                scored.append((score, comparison))
        for score, comparison in sorted(scored, key=lambda item: item[0], reverse=True)[
            :MAX_MATCHES_PER_PARAGRAPH
        ]:
            matches.append(_match_payload(match_index, draft, comparison, score))
            match_index += 1
    return matches


def _draft_paragraphs(manuscript: str) -> list[TextRecord]:
    records: list[TextRecord] = []
    for index, paragraph in enumerate(_paragraph_spans(manuscript), start=1):
        records.append(
            TextRecord(
                record_id=f"draft-p{index:03d}",
                kind="draft",
                title="Draft manuscript",
                text=paragraph.text,
                start=paragraph.start,
                end=paragraph.end,
                paragraph_index=index,
            ),
        )
    return records


def _comparison_paragraphs(
    *,
    run_dir: Path,
    user_id: str | None,
    session: Session | None,
    shortlist: Sequence[NormalizedSource],
    project: Project | None = None,
) -> list[TextRecord]:
    records: list[TextRecord] = []
    records.extend(
        _prior_corpus_paragraphs(user_id=user_id, project=project, session=session),
    )
    records.extend(_shortlist_source_paragraphs(run_dir=run_dir, shortlist=shortlist))
    return records


def _prior_corpus_paragraphs(
    user_id: str | None,
    session: Session | None,
    project: Project | None = None,
) -> list[TextRecord]:
    """Paragraphs from prior-paper corpora available to this run.

    Codex audit 2026-05-01 (PR-B1 soft): when ``project`` is given,
    use the per-project selection model (project-scoped + selected
    globals). When only ``user_id`` is given (legacy callers, tests)
    fall back to the owner-scoped global filter so behaviour stays
    compatible with pre-PR-B1.
    """
    if session is None:
        return []
    if project is not None:
        from autoessay.corpus import corpora_for_project

        corpora = corpora_for_project(session, project)
        if not corpora:
            return []
        corpus_ids = [corpus.id for corpus in corpora]
        stmt = (
            select(CorpusDocument)
            .where(
                CorpusDocument.corpus_id.in_(corpus_ids),
                CorpusDocument.ingest_status.in_(("extracted", "profiled")),
                CorpusDocument.extracted_text_path.is_not(None),
            )
            .order_by(CorpusDocument.created_at.desc(), CorpusDocument.id.desc())
        )
    elif user_id is not None:
        stmt = (
            select(CorpusDocument)
            .join(Corpus, CorpusDocument.corpus_id == Corpus.id)
            .where(
                or_(Corpus.user_id == user_id, Corpus.owner_user_id == user_id),
                Corpus.enabled.is_(True),
                CorpusDocument.ingest_status.in_(("extracted", "profiled")),
                CorpusDocument.extracted_text_path.is_not(None),
            )
            .order_by(CorpusDocument.created_at.desc(), CorpusDocument.id.desc())
        )
    else:
        return []
    documents = list(session.scalars(stmt))
    records: list[TextRecord] = []
    for document in documents:
        path = Path(str(document.extracted_text_path))
        text = _read_text(path)
        for index, paragraph in enumerate(_paragraph_texts(text), start=1):
            records.append(
                TextRecord(
                    record_id=f"prior:{document.id}:p{index:03d}",
                    kind="prior_corpus",
                    title=document.title,
                    text=paragraph,
                    document_id=document.id,
                    paragraph_index=index,
                ),
            )
    return records


def _shortlist_source_paragraphs(
    *,
    run_dir: Path,
    shortlist: Sequence[NormalizedSource],
) -> list[TextRecord]:
    records: list[TextRecord] = []
    for source in shortlist:
        text = _source_text(run_dir, source)
        for index, paragraph in enumerate(_paragraph_texts(text), start=1):
            records.append(
                TextRecord(
                    record_id=f"source:{source.source_id}:p{index:03d}",
                    kind="shortlist_source",
                    title=source.title,
                    text=paragraph,
                    source_id=source.source_id,
                    paragraph_index=index,
                ),
            )
    return records


def _source_text(run_dir: Path, source: NormalizedSource) -> str:
    manifest = _load_json_mapping(run_dir / "sources" / "fulltext_manifest.json")
    entry = manifest.get(source.source_id)
    if isinstance(entry, dict):
        for key in ("text_path", "extracted_text_path"):
            raw_path = entry.get(key)
            if isinstance(raw_path, str) and raw_path:
                text = _read_text(_resolve_run_path(run_dir, raw_path))
                if text:
                    return text
        raw_pdf_path = entry.get("pdf_path")
        if isinstance(raw_pdf_path, str) and raw_pdf_path:
            text = _extract_pdf_text(_resolve_run_path(run_dir, raw_pdf_path), source.source_id)
            if text:
                return text

    safe_id = _safe_filename(source.source_id)
    for relative_path in (
        Path("sources") / "extracted" / f"{safe_id}.txt",
        Path("sources") / "fulltext" / f"{safe_id}.txt",
        Path("sources") / "fulltext" / f"{source.source_id}.txt",
    ):
        text = _read_text(run_dir / relative_path)
        if text:
            return text

    note_text = _source_note_text(run_dir / "synthesis" / "source_notes" / f"{safe_id}.json")
    if note_text:
        return note_text
    return source.abstract or ""


def _source_note_text(path: Path) -> str:
    note = _load_json_mapping(path)
    if not note:
        return ""
    parts: list[str] = []
    for key in ("thesis", "method", "evidence", "limits"):
        value = note.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    claims = note.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict) and isinstance(claim.get("text"), str):
                parts.append(str(claim["text"]).strip())
    return "\n\n".join(part for part in parts if part)


def _extract_pdf_text(path: Path, source_id: str) -> str:
    try:
        return pdf_text.extract_text(path.read_bytes(), source_id=source_id)
    except (OSError, pdf_text.PoorExtraction):
        return ""


def _paragraph_spans(text: str) -> list[ParagraphSpan]:
    spans: list[ParagraphSpan] = []
    for match in re.finditer(r"(?ms)(^|\n\s*\n)(.+?)(?=\n\s*\n|\Z)", text):
        raw = match.group(2)
        stripped = raw.strip()
        if not _is_content_paragraph(stripped):
            continue
        offset = raw.find(stripped)
        start = match.start(2) + offset
        spans.append(
            ParagraphSpan(
                text=_normalize_paragraph(stripped),
                start=start,
                end=start + len(stripped),
            )
        )
    return spans


def _paragraph_texts(text: str) -> list[str]:
    return [item.text for item in _paragraph_spans(text)]


def _is_content_paragraph(value: str) -> bool:
    if len(value) < MIN_PARAGRAPH_CHARS:
        return False
    if value.startswith("#") or value.startswith("<a id=") or value.startswith("```"):
        return False
    return not (value.startswith("|") and value.endswith("|"))


def _normalize_paragraph(value: str) -> str:
    return " ".join(line.strip() for line in value.splitlines() if line.strip())


def _cosine(first: Sequence[float], second: Sequence[float]) -> float:
    if not first or not second or len(first) != len(second):
        return 0.0
    dot = sum(left * right for left, right in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0.0 or second_norm == 0.0:
        return 0.0
    return dot / (first_norm * second_norm)


def _payload(
    *,
    run_id: str,
    status: str,
    draft_records: Sequence[TextRecord],
    comparison_records: Sequence[TextRecord],
    matches: Sequence[dict[str, object]],
    threshold: float,
    reason: str | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "status": status,
        "model": MODEL_NAME,
        "threshold": threshold,
        "draft_paragraphs": len(draft_records),
        "comparison_paragraphs": len(comparison_records),
        "matches": list(matches),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        payload["reason"] = reason
    return payload


def _match_payload(
    match_index: int,
    draft: TextRecord,
    comparison: TextRecord,
    score: float,
) -> dict[str, object]:
    return {
        "match_id": f"local_dedup_{match_index:03d}",
        "risk": "self_plagiarism" if comparison.kind == "prior_corpus" else "source_copy",
        "similarity": round(score, 6),
        "draft_span": {
            "span_id": draft.record_id,
            "start": draft.start,
            "end": draft.end,
            "paragraph_index": draft.paragraph_index,
            "text": draft.text,
        },
        "attribution": {
            "kind": comparison.kind,
            "title": comparison.title,
            "source_id": comparison.source_id,
            "document_id": comparison.document_id,
            "paragraph_index": comparison.paragraph_index,
        },
        "matched_text": comparison.text,
    }


def _record_payload(record: TextRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "kind": record.kind,
        "title": record.title,
        "text": record.text,
        "start": record.start,
        "end": record.end,
        "source_id": record.source_id,
        "document_id": record.document_id,
        "paragraph_index": record.paragraph_index,
    }


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _read_text(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _resolve_run_path(run_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return run_dir / path


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "source"


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
