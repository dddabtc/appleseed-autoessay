"""Canned Scout fixtures for tests and smoke deployments."""

import hashlib

from autoessay.clients.common import AccessStatus, AsyncLitClient, NormalizedSource


class StubLitClient(AsyncLitClient):
    def __init__(self, source_id: str) -> None:
        super().__init__(source_id=source_id, min_interval_seconds=0.0, max_concurrency=10)

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del year_window
        return stub_sources(self.source_id, query)[:limit]


def stub_sources(source_client: str, query: str) -> list[NormalizedSource]:
    suffix = hashlib.sha1(f"{source_client}:{query}".encode()).hexdigest()[:10]
    return [
        NormalizedSource(
            source_id=f"{source_client}:shared-doi",
            title="Banking Panics and Credit Market Freezes",
            authors=["Anna Historian", "M. Credit"],
            year=2022,
            venue="Journal of Financial History",
            doi="10.5555/shared-scout",
            url=f"https://example.test/{source_client}/shared",
            pdf_url=f"https://example.test/{source_client}/shared.pdf",
            abstract=f"Stub result for {query}.",
            source_client=source_client,
            access_status=AccessStatus.OPEN,
            license="CC-BY-4.0",
            rank_score=0.91,
            risk_flags=[],
        ),
        NormalizedSource(
            source_id=f"{source_client}:{suffix}",
            title=f"Financial History Evidence for {query.title()}",
            authors=["Case Researcher"],
            year=2021,
            venue="Economic History Working Papers",
            doi=None,
            url=f"https://example.test/{source_client}/{suffix}",
            pdf_url=None,
            abstract="A metadata-only stub record for Scout.",
            source_client=source_client,
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.63,
            risk_flags=["stub_fixture"],
        ),
    ]
