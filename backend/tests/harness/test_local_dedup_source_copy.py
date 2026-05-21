import json
from pathlib import Path

from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.harness.dedup import run_local_dedup


class KeywordEmbedder:
    def embed(self, texts):  # type: ignore[no-untyped-def]
        vectors = []
        for text in texts:
            lowered = text.casefold()
            if "clearinghouse networks shaped liquidity responses" in lowered:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([1.0, 0.0, 0.0])
        return vectors


def test_local_dedup_flags_shortlist_source_copy(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run_local_dedup_source"
    run_dir.mkdir(parents=True)
    manuscript = (
        "## Body\n\n"
        "Clearinghouse networks shaped liquidity responses in local credit markets "
        "by coordinating reserves and limiting panic-driven withdrawals."
    )
    source = NormalizedSource(
        source_id="source_001",
        title="Clearinghouse Networks",
        authors=["A. Historian"],
        year=2020,
        venue="Journal of Economic History",
        doi="10.1000/source-copy",
        url="https://example.test/source",
        pdf_url=None,
        abstract=(
            "Clearinghouse networks shaped liquidity responses in local credit markets "
            "by coordinating reserves and limiting panic-driven withdrawals."
        ),
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )

    payload = run_local_dedup(
        run_id="run_local_dedup_source",
        run_dir=run_dir,
        user_id=None,
        session=None,
        manuscript=manuscript,
        shortlist=[source],
        embedder=KeywordEmbedder(),
    )

    artifact = json.loads((run_dir / "integrity" / "local_dedup.json").read_text(encoding="utf-8"))

    assert payload["status"] == "ok"
    assert artifact["matches"]
    assert artifact["matches"][0]["risk"] == "source_copy"
    assert artifact["matches"][0]["similarity"] == 1.0
    assert artifact["matches"][0]["attribution"]["source_id"] == "source_001"
    assert artifact["matches"][0]["attribution"]["title"] == "Clearinghouse Networks"


def test_local_dedup_stub_mode_writes_no_match_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_LOCAL_DEDUP_STUB", "1")
    get_settings.cache_clear()
    run_dir = tmp_path / "runs" / "run_local_dedup_stub"
    run_dir.mkdir(parents=True)

    payload = run_local_dedup(
        run_id="run_local_dedup_stub",
        run_dir=run_dir,
        user_id=None,
        session=None,
        manuscript="## Body\n\nA sufficiently long paragraph exists for the stubbed scan.",
        shortlist=[],
        embedder=KeywordEmbedder(),
    )

    artifact = json.loads((run_dir / "integrity" / "local_dedup.json").read_text(encoding="utf-8"))

    assert payload["status"] == "stubbed"
    assert artifact["matches"] == []
    assert artifact["reason"] == "AUTOESSAY_LOCAL_DEDUP_STUB=1"
    get_settings.cache_clear()
