"""Local prior-paper corpus ingestion and profile jobs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from autoessay.clients import docx_text, pdf_text
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.models import Corpus, CorpusDocument, Project, User
from autoessay.style_profile import build_style_profile_from_paths, style_profile_summary

MAX_CORPUS_UPLOAD_BYTES = 30 * 1024 * 1024
ALLOWED_CONTENT_TYPES: dict[str, set[str]] = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/octet-stream",
    },
    ".md": {"text/markdown", "text/plain"},
    ".txt": {"text/plain"},
}


class CorpusUploadError(ValueError):
    pass


def create_corpus_document(
    session: Session,
    user: User,
    *,
    filename: str,
    content_type: str | None,
    payload: bytes,
) -> CorpusDocument:
    extension = _validated_extension(filename, content_type)
    if len(payload) > MAX_CORPUS_UPLOAD_BYTES:
        raise CorpusUploadError("file exceeds 30 MB upload limit")
    if len(payload) == 0:
        raise CorpusUploadError("file is empty")

    ensure_user(session, user)
    corpus = ensure_user_corpus(session, user.id)
    document_hash = hashlib.sha256(payload).hexdigest()
    existing = session.scalar(
        select(CorpusDocument).where(
            CorpusDocument.corpus_id == corpus.id,
            CorpusDocument.document_hash == document_hash,
        ),
    )
    if existing is not None:
        raise CorpusUploadError("document already exists in corpus")

    original_dir = _user_corpus_dir(user.id) / "originals"
    original_dir.mkdir(parents=True, exist_ok=True)
    source_path = original_dir / f"{document_hash}{extension}"
    source_path.write_bytes(payload)
    document = CorpusDocument(
        id=f"doc_{uuid4().hex}",
        corpus_id=corpus.id,
        title=_title_from_filename(filename),
        source_path=str(source_path),
        document_type="prior_paper",
        document_hash=document_hash,
        original_size_bytes=len(payload),
        extracted_text_path=None,
        style_profile_path=None,
        privacy_level="private",
        ingest_status="pending",
        sensitivity="private",
        metadata_json={
            "filename": Path(filename).name,
            "content_type": content_type or "",
            "extension": extension.removeprefix("."),
        },
    )
    session.add(document)
    session.flush()
    return document


def ensure_user(session: Session, user: User) -> None:
    if session.get(User, user.id) is not None:
        return
    session.add(
        User(
            id=user.id,
            oidc_subject=user.oidc_subject,
            oidc_issuer=user.oidc_issuer,
            email=user.email,
            display_name=user.display_name or user.id,
            picture_url=user.picture_url,
        ),
    )
    session.flush()


def ensure_user_corpus(session: Session, user_id: str) -> Corpus:
    """Get-or-create the user's GLOBAL corpus.

    Codex audit 2026-05-01: ``project_id IS NULL`` filter is
    mandatory here. Without it, if the user happens to have only a
    project-scoped corpus, this helper would return *that* corpus
    and ``/api/corpus/upload`` (the global upload endpoint) would
    silently land global uploads in the project corpus.
    """
    corpus = session.scalar(
        select(Corpus)
        .where(
            _corpus_owner_filter(user_id),
            Corpus.project_id.is_(None),
            Corpus.enabled.is_(True),
        )
        .order_by(Corpus.created_at.asc())
        .limit(1),
    )
    if corpus is not None:
        if corpus.user_id is None:
            corpus.user_id = user_id
        return corpus
    corpus = Corpus(
        id=f"corp_{uuid4().hex}",
        owner_user_id=user_id,
        user_id=user_id,
        name="Prior papers",
        enabled=True,
    )
    session.add(corpus)
    session.flush()
    return corpus


def ensure_project_corpus(session: Session, project: Project) -> Corpus:
    """Get-or-create the single project-scoped corpus for ``project``.

    Project-scoped corpora are conceptually one-per-project (the
    workspace's "Corpus" sub-tab uploads land in this corpus). If
    a user later wants multiple named project corpora that's a
    follow-up; today every project has one implicit project-scoped
    corpus named ``"Project corpus"``.
    """
    corpus = session.scalar(
        select(Corpus)
        .where(
            Corpus.owner_user_id == project.user_id,
            Corpus.project_id == project.id,
            Corpus.enabled.is_(True),
        )
        .order_by(Corpus.created_at.asc())
        .limit(1),
    )
    if corpus is not None:
        return corpus
    corpus = Corpus(
        id=f"corp_{uuid4().hex}",
        owner_user_id=project.user_id,
        user_id=project.user_id,
        project_id=project.id,
        name="Project corpus",
        enabled=True,
    )
    session.add(corpus)
    session.flush()
    return corpus


def create_project_corpus_document(
    session: Session,
    user: User,
    project: Project,
    *,
    filename: str,
    content_type: str | None,
    payload: bytes,
) -> CorpusDocument:
    """Project-scoped variant of ``create_corpus_document``. Uploads
    land in the per-project corpus (``Corpus.project_id == project.id``)
    instead of the user's global corpus."""
    extension = _validated_extension(filename, content_type)
    if len(payload) > MAX_CORPUS_UPLOAD_BYTES:
        raise CorpusUploadError("file exceeds 30 MB upload limit")
    if len(payload) == 0:
        raise CorpusUploadError("file is empty")
    ensure_user(session, user)
    corpus = ensure_project_corpus(session, project)
    document_hash = hashlib.sha256(payload).hexdigest()
    existing = session.scalar(
        select(CorpusDocument).where(
            CorpusDocument.corpus_id == corpus.id,
            CorpusDocument.document_hash == document_hash,
        ),
    )
    if existing is not None:
        raise CorpusUploadError("document already exists in this project's corpus")
    original_dir = _user_corpus_dir(user.id) / "projects" / project.id / "originals"
    original_dir.mkdir(parents=True, exist_ok=True)
    source_path = original_dir / f"{document_hash}{extension}"
    source_path.write_bytes(payload)
    document = CorpusDocument(
        id=f"doc_{uuid4().hex}",
        corpus_id=corpus.id,
        title=_title_from_filename(filename),
        source_path=str(source_path),
        document_type="prior_paper",
        document_hash=document_hash,
        original_size_bytes=len(payload),
        extracted_text_path=None,
        style_profile_path=None,
        privacy_level="private",
        ingest_status="pending",
        sensitivity="private",
        metadata_json={
            "filename": Path(filename).name,
            "content_type": content_type or "",
            "extension": extension.removeprefix("."),
            "project_id": project.id,
        },
    )
    session.add(document)
    session.flush()
    return document


def list_user_documents(session: Session, user_id: str) -> list[CorpusDocument]:
    """List documents in the user's GLOBAL corpora only.

    Codex audit 2026-05-01: project-scoped corpora must NOT show up
    in the global ``/api/corpus`` listing — they belong to a
    specific project's corpus tab. Without the ``project_id IS NULL``
    filter the global listing would leak project documents to the
    global view (still owner-scoped, so no cross-user issue, but a
    UX / privacy-boundary problem within one user's view).
    """
    return list(
        session.scalars(
            select(CorpusDocument)
            .join(Corpus, CorpusDocument.corpus_id == Corpus.id)
            .where(
                _corpus_owner_filter(user_id),
                Corpus.project_id.is_(None),
                Corpus.enabled.is_(True),
            )
            .order_by(CorpusDocument.created_at.desc(), CorpusDocument.id.desc()),
        ),
    )


def get_user_document(session: Session, user_id: str, document_id: str) -> CorpusDocument | None:
    return session.scalar(
        select(CorpusDocument)
        .join(Corpus, CorpusDocument.corpus_id == Corpus.id)
        .where(
            CorpusDocument.id == document_id,
            _corpus_owner_filter(user_id),
            Corpus.enabled.is_(True),
        ),
    )


def delete_document_files(document: CorpusDocument) -> None:
    _remove_file(document.source_path)
    if document.extracted_text_path:
        _remove_file(document.extracted_text_path)


def run_corpus_ingest_job(
    document_id: str,
    db_session: Session | None = None,
) -> dict[str, object]:
    if db_session is not None:
        return _run_corpus_ingest_job_with_session(document_id, db_session)
    with SessionLocal() as session:
        return _run_corpus_ingest_job_with_session(document_id, session)


def run_corpus_style_profile_job(
    user_id: str,
    db_session: Session | None = None,
) -> dict[str, object]:
    if db_session is not None:
        return _run_corpus_style_profile_job_with_session(user_id, db_session)
    with SessionLocal() as session:
        return _run_corpus_style_profile_job_with_session(user_id, session)


def style_profile_path_for_user(user_id: str) -> Path:
    return _user_corpus_dir(user_id) / "style_profile.json"


def preview_document_text(document: CorpusDocument, max_chars: int) -> str:
    capped = min(max(max_chars, 0), 200)
    path = Path(document.extracted_text_path or document.source_path)
    if not path.exists():
        return ""
    if document.extracted_text_path:
        return path.read_text(encoding="utf-8", errors="ignore")[:capped]
    try:
        text = _extract_text_from_path(path, document)
    except pdf_text.PoorExtraction:
        text = path.read_bytes().decode("utf-8", errors="ignore")
    return text[:capped]


def _run_corpus_ingest_job_with_session(
    document_id: str,
    session: Session,
) -> dict[str, object]:
    document = session.get(CorpusDocument, document_id)
    if document is None:
        raise ValueError(f"corpus document not found: {document_id}")
    document.ingest_status = "extracting"
    session.commit()
    try:
        source_path = Path(document.source_path)
        extracted = _extract_text_from_path(source_path, document)
        extracted_dir = source_path.parent.parent / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        document_hash = (
            document.document_hash
            or hashlib.sha256(
                source_path.read_bytes(),
            ).hexdigest()
        )
        extracted_path = extracted_dir / f"{document_hash}.txt"
        extracted_path.write_text(extracted, encoding="utf-8")
        document.extracted_text_path = str(extracted_path)
        document.ingest_status = "extracted"
        document.metadata_json = {
            **(document.metadata_json or {}),
            "extracted_chars": len(extracted),
        }
        session.commit()
        return {
            "document_id": document.id,
            "ingest_status": document.ingest_status,
            "extracted_chars": len(extracted),
        }
    except Exception as exc:  # noqa: BLE001 - job records deterministic status for API.
        document.ingest_status = "failed"
        document.metadata_json = {
            **(document.metadata_json or {}),
            "ingest_error": str(exc),
        }
        session.commit()
        return {"document_id": document.id, "ingest_status": "failed", "error": str(exc)}


def _run_corpus_style_profile_job_with_session(
    user_id: str,
    session: Session,
) -> dict[str, object]:
    documents = [
        document
        for document in list_user_documents(session, user_id)
        if document.ingest_status in {"extracted", "profiled"} and document.extracted_text_path
    ]
    paths = [Path(str(document.extracted_text_path)) for document in documents]
    profile = build_style_profile_from_paths(paths, allow_prior_text=False)
    payload = style_profile_summary(profile)
    profile_path = style_profile_path_for_user(user_id)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for document in documents:
        document.ingest_status = "profiled"
        document.style_profile_path = str(profile_path)
    session.commit()
    return {
        "user_id": user_id,
        "document_count": len(documents),
        "style_profile_path": str(profile_path),
    }


def _extract_text_from_path(path: Path, document: CorpusDocument) -> str:
    extension = _document_extension(path, document)
    payload = path.read_bytes()
    if extension == ".pdf":
        return pdf_text.extract_text(payload, source_id=document.id)
    if extension == ".docx":
        return docx_text.extract_text(payload, source_id=document.id)
    return payload.decode("utf-8", errors="ignore")


def _document_extension(path: Path, document: CorpusDocument) -> str:
    raw_extension = ""
    metadata = document.metadata_json or {}
    metadata_extension = metadata.get("extension")
    if isinstance(metadata_extension, str) and metadata_extension:
        raw_extension = metadata_extension
    if not raw_extension:
        raw_extension = path.suffix
    raw_extension = raw_extension if raw_extension.startswith(".") else f".{raw_extension}"
    return raw_extension.casefold()


def _validated_extension(filename: str, content_type: str | None) -> str:
    extension = Path(filename).suffix.casefold()
    if extension not in ALLOWED_CONTENT_TYPES:
        raise CorpusUploadError("unsupported file type")
    normalized_type = _normalize_content_type(content_type)
    if normalized_type not in ALLOWED_CONTENT_TYPES[extension]:
        raise CorpusUploadError("file extension and content type do not match")
    return extension


def _normalize_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().casefold()


def _title_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    title = re.sub(r"[_-]+", " ", stem).strip()
    return title or "Untitled prior paper"


def _user_corpus_dir(user_id: str) -> Path:
    return get_settings().data_dir / "runs" / user_id / "corpus"


def _corpus_owner_filter(user_id: str) -> ColumnElement[bool]:
    return or_(Corpus.user_id == user_id, Corpus.owner_user_id == user_id)


def corpora_for_project(session: Session, project: Project) -> list[Corpus]:
    """Return the effective list of enabled corpora that style_profile
    + dedup queries should draw from for ``project``.

    The effective list is the union of:

    - every project-scoped corpus owned by this project
      (``Corpus.project_id == project.id``)
    - every global corpus (``Corpus.project_id IS NULL``) that the
      project explicitly selected via ``ProjectCorpusSelection``

    Always enforces owner integrity (``Corpus.owner_user_id ==
    project.user_id``). Codex amendment 2 to issue 2 of the
    2026-05-01 design review:

    > Scope query should always enforce owner.

    Codex amendment 1 to issue 2:

    > "Can select from global" needs an explicit selection model,
    > not only automatic global+project union.

    Migration 016 backfilled selections for every existing project
    so legacy projects continue to see their owner's global corpora
    without explicit user action.
    """
    from autoessay.models import ProjectCorpusSelection

    project_owned = select(Corpus).where(
        Corpus.owner_user_id == project.user_id,
        Corpus.project_id == project.id,
        Corpus.enabled.is_(True),
    )
    selected_global = (
        select(Corpus)
        .join(
            ProjectCorpusSelection,
            ProjectCorpusSelection.corpus_id == Corpus.id,
        )
        .where(
            ProjectCorpusSelection.project_id == project.id,
            Corpus.owner_user_id == project.user_id,
            Corpus.project_id.is_(None),
            Corpus.enabled.is_(True),
        )
    )
    rows = list(session.scalars(project_owned).all()) + list(
        session.scalars(selected_global).all(),
    )
    seen: set[str] = set()
    deduped: list[Corpus] = []
    for corpus in rows:
        if corpus.id in seen:
            continue
        seen.add(corpus.id)
        deduped.append(corpus)
    return deduped


def _remove_file(path_value: str) -> None:
    path = Path(path_value)
    if path.exists() and path.is_file():
        path.unlink()
