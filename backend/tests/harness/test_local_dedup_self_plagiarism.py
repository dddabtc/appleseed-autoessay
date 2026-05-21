import json
from pathlib import Path

from autoessay.harness.dedup import run_local_dedup
from autoessay.models import Corpus, CorpusDocument, User


class KeywordEmbedder:
    def embed(self, texts):  # type: ignore[no-untyped-def]
        vectors = []
        for text in texts:
            lowered = text.casefold()
            if "deposit insurance shifted bank incentives" in lowered:
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 1.0, 0.0])
        return vectors


def test_local_dedup_flags_prior_corpus_self_plagiarism(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run_local_dedup_self"
    run_dir.mkdir(parents=True)
    prior_text_path = tmp_path / "prior.txt"
    prior_text_path.write_text(
        "Deposit insurance shifted bank incentives during interwar banking stress "
        "by changing how depositors priced institutional risk across local markets.",
        encoding="utf-8",
    )
    manuscript = (
        "## Introduction\n\n"
        "Deposit insurance shifted bank incentives during interwar banking stress "
        "by changing how depositors priced institutional risk across local markets."
    )

    with app_session() as session:
        session.add(User(id="single-user", display_name="Single User"))
        session.flush()
        session.add(
            Corpus(
                id="corp_prior",
                owner_user_id="single-user",
                user_id="single-user",
                name="Prior papers",
                enabled=True,
            ),
        )
        session.add(
            CorpusDocument(
                id="doc_prior",
                corpus_id="corp_prior",
                title="Prior Banking Paper",
                source_path=str(prior_text_path),
                document_type="prior_paper",
                document_hash="hash-prior",
                original_size_bytes=prior_text_path.stat().st_size,
                extracted_text_path=str(prior_text_path),
                privacy_level="private",
                ingest_status="extracted",
                sensitivity="private",
                metadata_json={},
            ),
        )
        session.commit()

        payload = run_local_dedup(
            run_id="run_local_dedup_self",
            run_dir=run_dir,
            user_id="single-user",
            session=session,
            manuscript=manuscript,
            shortlist=[],
            embedder=KeywordEmbedder(),
        )

    artifact = json.loads((run_dir / "integrity" / "local_dedup.json").read_text(encoding="utf-8"))

    assert payload["status"] == "ok"
    assert artifact["matches"]
    assert artifact["matches"][0]["risk"] == "self_plagiarism"
    assert artifact["matches"][0]["similarity"] == 1.0
    assert artifact["matches"][0]["attribution"]["document_id"] == "doc_prior"
    assert artifact["matches"][0]["attribution"]["title"] == "Prior Banking Paper"
