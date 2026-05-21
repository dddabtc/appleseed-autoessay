from pathlib import Path

from autoessay.agents.scout import _enabled_source_configs
from autoessay.domain_loader import load_domains


def test_all_repo_domains_load_and_pass_schema() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    domains = load_domains(repo_root / "domains")

    assert {"financial_history", "economic_history", "general_academic"} <= set(domains)
    for domain_id in ("financial_history", "economic_history", "general_academic"):
        assert domains[domain_id].warnings == ()
    financial_sources = _enabled_source_configs(domains["financial_history"].data)
    economic_sources = _enabled_source_configs(domains["economic_history"].data)
    general_sources = _enabled_source_configs(domains["general_academic"].data)
    assert any(source["id"] == "cnki" for source in financial_sources)
    assert any(source["id"] == "openalex" for source in financial_sources)
    assert any(source["id"] == "openalex" for source in economic_sources)
    assert any(source["id"] == "openalex" for source in general_sources)
