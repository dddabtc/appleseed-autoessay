from pathlib import Path

from conftest import seed_project

from autoessay.models import Corpus, CorpusDocument, ProjectCorpusSelection
from autoessay.style_profile import build_style_profile


def test_style_profile_from_fake_prior_pdfs_is_aggregate_only(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    first_text = (
        "Financial archives may show how depositors changed behavior. "
        "The bank correspondence suggests a slower institutional response.\n\n"
        "Financial archives may also expose regional credit terms."
    )
    second_text = (
        "Monetary committees could delay interventions when reserves narrowed. "
        "The archival ledger suggests credit policy shifted unevenly.\n\n"
        "Monetary committees could cite precedent before changing rules."
    )
    first_path = tmp_path / "prior-one.pdf"
    second_path = tmp_path / "prior-two.pdf"
    first_path.write_text(first_text, encoding="utf-8")
    second_path.write_text(second_text, encoding="utf-8")

    with app_session() as session:
        project = seed_project(session)
        session.add(Corpus(id="corp_1", owner_user_id=project.user_id, name="Prior papers"))
        # PR-B1: corpora_for_project requires an explicit selection
        # row for global corpora. New projects auto-include current
        # globals via the API path, but this test seeds the corpus
        # AFTER seed_project, so we add the selection by hand.
        session.add(
            ProjectCorpusSelection(project_id=project.id, corpus_id="corp_1"),
        )
        session.add(
            CorpusDocument(
                id="doc_1",
                corpus_id="corp_1",
                title="Prior one",
                source_path=str(first_path),
                document_type="prior_paper",
                sensitivity="private",
                metadata_json={},
            ),
        )
        session.add(
            CorpusDocument(
                id="doc_2",
                corpus_id="corp_1",
                title="Prior two",
                source_path=str(second_path),
                document_type="prior_paper",
                sensitivity="private",
                metadata_json={},
            ),
        )
        session.commit()

        profile = build_style_profile(session, project, allow_prior_text=False)

    serialized = profile.json()
    assert profile.paragraph_length_distribution.mean > 0
    assert profile.sentence_length_distribution.mean > 0
    assert profile.opener_patterns
    assert "may" in profile.hedging_patterns or "could" in profile.hedging_patterns
    assert profile.common_domain_terms
    assert profile.short_local_examples == []
    assert first_text not in serialized
    assert second_text not in serialized


def test_style_profile_detects_chinese_and_extracts_domain_terms() -> None:
    """PR-B2: build_style_profile_from_texts on Chinese input now
    detects ``zh``, segments via jieba, surfaces real Chinese
    domain terms + Chinese hedging phrases, and reports diagnostics."""
    from autoessay.style_profile import build_style_profile_from_texts

    chinese_corpus = [
        (
            "新教与市场经济的耦合度研究是金融历史学的重要议题。"
            "本文档讨论了清教伦理对资本积累的可能影响，倾向于支持韦伯的论点。\n\n"
            "另一段考察了改革宗神学与现代银行业的关系，似乎反映出制度共演的特征。"
        ),
        (
            "本研究的第二份语料关注新教与市场经济的长期相关性。"
            "数据估计显示了显著的正向关系，应该谨慎解读因果链条。\n\n"
            "结论部分讨论了未来的研究方向。"
        ),
    ]

    profile = build_style_profile_from_texts(chinese_corpus, allow_prior_text=False)

    assert profile.detected_language == "zh"
    assert profile.document_count == 2
    assert profile.total_token_count > 0
    assert profile.paragraph_length_distribution.mean > 0
    # Chinese hedging matched (multiple options to keep stable
    # against jieba dictionary updates).
    assert any(
        phrase in profile.hedging_patterns for phrase in ("可能", "似乎", "倾向", "估计", "应该")
    )
    # At least one real domain term should appear (jieba reliably
    # segments these multi-character compounds).
    assert any(term in profile.common_domain_terms for term in ("新教", "市场经济", "研究"))
    # Empty-section diagnostics absent for a healthy corpus.
    assert profile.empty_section_warnings == []


def test_style_profile_diagnostics_for_empty_corpus() -> None:
    """No documents → diagnostics surface the reason instead of
    silently returning a zeroed profile."""
    from autoessay.style_profile import build_style_profile_from_texts

    profile = build_style_profile_from_texts([], allow_prior_text=False)
    assert profile.detected_language == "unknown"
    assert profile.document_count == 0
    assert profile.empty_section_warnings == ["no documents provided"]


def test_detect_language_handles_mixed_inputs() -> None:
    from autoessay.style_profile import detect_language

    assert detect_language("This is purely English prose.") == "en"
    assert detect_language("これは日本語のテキストです。") == "ja"
    assert detect_language("这是一段纯中文。研究新教与经济。") == "zh"
    # Mixed but Han-dominant → zh.
    assert detect_language("This includes 中文 segments 比较 obvious 的研究.") == "zh"
    # Empty → en (default).
    assert detect_language("") == "en"


def test_zh_tokens_falls_back_to_bigrams_when_jieba_unavailable(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """PR-B2 codex audit soft: simulate jieba ImportError so the
    bigram fallback is exercised. The profile must still surface
    SOMETHING for `common_domain_terms` rather than empty out."""
    import builtins

    from autoessay.style_profile import build_style_profile_from_texts

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "jieba":
            raise ImportError("simulated missing jieba")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    profile = build_style_profile_from_texts(
        [
            "新教与市场经济的耦合度研究是金融历史学的重要议题。\n\n本研究讨论了清教伦理对资本积累的可能影响。"
        ],
        allow_prior_text=False,
    )

    assert profile.detected_language == "zh"
    # Bigram fallback should still produce non-empty output for
    # common_domain_terms (sliding 2-character window over the
    # CJK code-point subset).
    assert profile.common_domain_terms, (
        "jieba-missing fallback should still emit terms via _cjk_bigrams"
    )
    # Diagnostics surface the empty case via warnings; for healthy
    # input there should be none here.
    assert profile.empty_section_warnings == []
