from collections import Counter

from autoessay.agents.curator import diversity_rerank
from autoessay.clients.common import AccessStatus, NormalizedSource


def test_diversity_rerank_caps_venue_and_author() -> None:
    sources = [
        _source("same_venue_author_0", "Top Journal", ["Repeated Author"], 1.0),
        _source("same_venue_author_1", "Top Journal", ["Repeated Author"], 0.99),
        _source("same_venue_author_2", "Top Journal", ["Repeated Author"], 0.98),
        _source("same_venue_other_0", "Top Journal", ["Other A"], 0.97),
        _source("same_venue_other_1", "Top Journal", ["Other B"], 0.96),
        _source("venue_b", "Venue B", ["Author B"], 0.95),
        _source("venue_c", "Venue C", ["Author C"], 0.94),
        _source("venue_d", "Venue D", ["Author D"], 0.93),
        _source("venue_e", "Venue E", ["Author E"], 0.92),
        _source("venue_f", "Venue F", ["Author F"], 0.91),
    ]

    result = diversity_rerank(sources, limit=10)

    venue_counts = Counter(source.venue for source in result.selected)
    author_counts: Counter[str] = Counter()
    for source in result.selected:
        for author in source.authors:
            author_counts[author] += 1

    assert venue_counts["Top Journal"] == 3
    assert author_counts["Repeated Author"] == 2
    assert any(item["diversity_reject_reason"] == "author_cap" for item in result.runner_ups)
    assert any(item["diversity_reject_reason"] == "venue_cap" for item in result.runner_ups)


def _source(source_id: str, venue: str, authors: list[str], score: float) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=source_id,
        authors=authors,
        year=2024,
        venue=venue,
        doi=None,
        url=None,
        pdf_url=None,
        abstract=None,
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=score,
        risk_flags=[],
    )
