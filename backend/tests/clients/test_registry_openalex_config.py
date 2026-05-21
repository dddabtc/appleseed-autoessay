from autoessay.clients.registry import _openalex_domain_id, _openalex_filters


def test_configured_openalex_absent_filter_disables_default_filter() -> None:
    assert _openalex_filters({"id": "openalex"}) is None


def test_configured_openalex_can_disable_domain_topic_filter() -> None:
    assert (
        _openalex_domain_id(
            {"id": "openalex", "topic_filter": False},
            {"id": "financial_history"},
        )
        is None
    )


def test_unconfigured_openalex_keeps_client_default_filter() -> None:
    assert _openalex_filters(None) is not None
