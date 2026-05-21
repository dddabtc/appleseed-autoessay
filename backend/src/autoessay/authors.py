"""Author roster + per-project author management (codex-AGREEd #5).

The user maintains a personal roster of authors (display name +
affiliation + email + ORCID). Each project picks an ordered subset
of these authors; the exporter renders them in the manuscript front
matter.

This module owns:
- Validation helpers (display_name normalization, ORCID regex +
  checksum, optional email).
- Lazy ``self``-author bootstrap so the user always has at least one
  author available the first time they open settings.
- The ``set_project_authors`` transactional update — replaces a
  project's author list with full validation per codex's rules.

Caps:
- ROSTER_CAP_PER_USER = 200 (active authors only; soft-deleted don't count)
- AUTHORS_PER_PROJECT_CAP = 50

Soft-delete semantics: deleting an author flags ``deleted_at`` but
leaves any ``project_author`` rows intact, so historical manuscripts
still render the right author list. The author can no longer be
*newly* picked for a project — only re-attached to projects they're
already on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoessay.models import Author, Project, ProjectAuthor, User, utcnow

ROSTER_CAP_PER_USER = 200
AUTHORS_PER_PROJECT_CAP = 50

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _verify_orcid_checksum(orcid: str) -> bool:
    """Validate the ORCID mod-11-2 checksum.

    See https://support.orcid.org/hc/en-us/articles/360006897674. The
    checksum is the last character of the iD; the rest is digits.
    Cheap to compute and catches typos that the regex alone misses.
    """
    digits = orcid.replace("-", "")
    if len(digits) != 16:
        return False
    payload, check_char = digits[:15], digits[15]
    expected_check = "X" if check_char == "X" else check_char
    total = 0
    for ch in payload:
        if not ch.isdigit():
            return False
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    expected = "X" if result == 10 else str(result)
    return expected == expected_check


def normalize_display_name(value: str | None) -> str:
    """Trim and reject empty display names."""
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="display_name is required",
        )
    cleaned = " ".join(value.split())
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="display_name cannot be empty",
        )
    return cleaned


def normalize_optional_string(value: str | None) -> str | None:
    """Empty string -> None; otherwise trimmed."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def validate_orcid(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    if not _ORCID_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="orcid must match pattern 0000-0000-0000-000X (X = digit or X)",
        )
    if not _verify_orcid_checksum(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="orcid checksum failed",
        )
    return cleaned


def validate_email(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _EMAIL_RE.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email is not a valid address",
        )
    return cleaned


# ---------------------------------------------------------------------------
# Roster bootstrap & queries
# ---------------------------------------------------------------------------


def get_or_create_self_author(session: Session, user: User) -> Author:
    """Lazy-bootstrap the user's own author entry.

    Called the first time the user lists authors. Uses
    ``User.display_name`` as a starting point — the user can edit
    later. Idempotent: returns the existing self-author if one exists.

    Also persists the User row if it does not exist yet (the
    auth-bypass middleware hands us a stub User without committing
    it). The Author -> users FK requires the user row to be present.
    """
    persisted = session.get(User, user.id)
    if persisted is None:
        session.add(
            User(
                id=user.id,
                display_name=user.display_name,
                email=user.email,
            ),
        )
        session.flush()
    existing = session.scalar(
        select(Author)
        .where(Author.user_id == user.id, Author.is_self.is_(True))
        .where(Author.deleted_at.is_(None)),
    )
    if existing is not None:
        return existing
    author = Author(
        id=f"author_{uuid4().hex}",
        user_id=user.id,
        display_name=normalize_display_name(user.display_name or "Author"),
        is_self=True,
    )
    session.add(author)
    session.flush()
    return author


def list_authors(session: Session, user_id: str, *, include_deleted: bool = False) -> list[Author]:
    stmt = select(Author).where(Author.user_id == user_id)
    if not include_deleted:
        stmt = stmt.where(Author.deleted_at.is_(None))
    return list(
        session.scalars(stmt.order_by(Author.is_self.desc(), Author.created_at.asc())).all()
    )


def count_active_authors(session: Session, user_id: str) -> int:
    stmt = (
        select(func.count(Author.id))
        .where(Author.user_id == user_id)
        .where(Author.deleted_at.is_(None))
    )
    return int(session.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# Project author assignment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectAuthorEntry:
    author_id: str
    position: int


def get_project_authors(session: Session, project_id: str) -> list[tuple[ProjectAuthor, Author]]:
    """Return ordered ``(project_author, author)`` rows for a project."""
    stmt = (
        select(ProjectAuthor, Author)
        .join(Author, Author.id == ProjectAuthor.author_id)
        .where(ProjectAuthor.project_id == project_id)
        .order_by(ProjectAuthor.position.asc())
    )
    return [(pa, author) for pa, author in session.execute(stmt).all()]


def set_project_authors(
    session: Session,
    project: Project,
    user: User,
    entries: Sequence[ProjectAuthorEntry],
) -> list[tuple[ProjectAuthor, Author]]:
    """Replace the project's author list atomically.

    Validations (all 400/409 on failure):
    - project belongs to current user
    - max ``AUTHORS_PER_PROJECT_CAP`` entries
    - no duplicate author_id
    - positions form a contiguous 0..N-1 set
    - every author belongs to the current user
    - a soft-deleted author may appear ONLY if it is already attached
      to this same project (preserves historical manuscripts but
      blocks newly picking deleted authors)
    """
    if project.user_id != user.id:
        # Caller should have already 404'd; double-check defensively.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if len(entries) > AUTHORS_PER_PROJECT_CAP:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a project may have at most {AUTHORS_PER_PROJECT_CAP} authors",
        )
    if not entries:
        # Allow clearing — the exporter falls back to the self-author.
        existing = session.scalars(
            select(ProjectAuthor).where(ProjectAuthor.project_id == project.id),
        ).all()
        for row in existing:
            session.delete(row)
        session.flush()
        return []
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.author_id in seen_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"author {entry.author_id} appears more than once",
            )
        seen_ids.add(entry.author_id)
    positions = sorted(e.position for e in entries)
    if positions != list(range(len(entries))):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="positions must form a contiguous 0..N-1 set",
        )
    authors_by_id: dict[str, Author] = {
        a.id: a
        for a in session.scalars(
            select(Author).where(Author.user_id == user.id).where(Author.id.in_(seen_ids)),
        ).all()
    }
    if len(authors_by_id) != len(seen_ids):
        missing = seen_ids - authors_by_id.keys()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"author(s) not found in your roster: {sorted(missing)}",
        )
    already_attached = {
        row.author_id
        for row in session.scalars(
            select(ProjectAuthor).where(ProjectAuthor.project_id == project.id),
        ).all()
    }
    for author_id, author in authors_by_id.items():
        if author.deleted_at is not None and author_id not in already_attached:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"author {author_id} is deleted and cannot be newly added to a project"),
            )
    # Replace atomically: drop existing rows, insert new ones.
    existing_rows = session.scalars(
        select(ProjectAuthor).where(ProjectAuthor.project_id == project.id),
    ).all()
    for row in existing_rows:
        session.delete(row)
    session.flush()
    for entry in sorted(entries, key=lambda e: e.position):
        session.add(
            ProjectAuthor(
                project_id=project.id,
                author_id=entry.author_id,
                position=entry.position,
            ),
        )
    project.updated_at = utcnow()
    session.flush()
    return get_project_authors(session, project.id)


__all__ = [
    "AUTHORS_PER_PROJECT_CAP",
    "ROSTER_CAP_PER_USER",
    "ProjectAuthorEntry",
    "count_active_authors",
    "get_or_create_self_author",
    "get_project_authors",
    "list_authors",
    "normalize_display_name",
    "normalize_optional_string",
    "set_project_authors",
    "validate_email",
    "validate_orcid",
]
