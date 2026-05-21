from pathlib import Path

import pytest

from autoessay.domain_loader import DomainConfigError, load_domain


def test_loads_financial_history_domain() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    loaded = load_domain(repo_root / "domains" / "financial_history.yaml")

    assert loaded.data["id"] == "financial_history"
    assert loaded.data["display_name"] == "Financial History"
    assert loaded.data["search"]["sources"]
    assert loaded.data["journals"]["targets"]
    assert loaded.data["citation"]["style"] == "chicago_author_date"
    assert loaded.warnings == ()


def test_rejects_unknown_source_id(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
id: bad_domain
display_name: Bad Domain
version: "0.1.0"
search:
  sources:
    - id: unknown_source
      enabled: true
  default_query_terms: []
  exclusion_terms: []
journals:
  targets:
    - name: "Journal"
      expected_length_words: [1000, 2000]
citation:
  style: chicago_author_date
""",
        encoding="utf-8",
    )

    with pytest.raises(DomainConfigError, match="unknown search source_id"):
        load_domain(path)
