from __future__ import annotations

from autoessay.experiments.abc_extract import KernelMetadata
from autoessay.experiments.abc_prompts import (
    build_b_prime_anti_echo_retry_prompt,
    build_b_prime_prompt,
    build_b_prompt,
    build_c_prompt,
    build_g_ars_front_half_prompt,
)


def _kernel() -> KernelMetadata:
    return KernelMetadata(
        title="公共物品理论在中国财政学中的本土化路径",
        research_kernel={"tentative_question": "公共物品理论如何被本土化？"},
        target_journal="《经济研究》",
    )


def test_b_prompt_consumes_evidence_package_and_forbids_extra_retrieval() -> None:
    prompt = build_b_prompt(
        kernel=_kernel(),
        package_md="source_id: S1\nclaim",
        humanizer_directive="humanize zh",
    )

    text = prompt.as_text()
    assert "source_id: S1" in text
    assert "不进行额外检索" in text
    assert "证据包之外" in text
    assert "humanize zh" in text
    assert "drafter" not in text.lower()
    assert len(prompt.messages) == 2


def test_b_prime_prompt_is_targeted_local_fix_not_fresh_rewrite() -> None:
    prompt = build_b_prime_prompt(
        package_md="source_id: S1\nclaim",
        base_b_manuscript="# B\n\n正文 [1]\n",
        humanizer_directive="humanize zh",
    )

    text = prompt.as_text()
    assert "self-critique-and-targeted-revision" in text
    assert "至少 3 处可改进" in text
    assert "直接 echo B 稿原文不被接受" in text
    assert "不能 100% echo 原文" in text
    assert "不要输出 self-critique 内容" in text
    assert "targeted local fixes" in text
    assert "不是重新写一篇新论文" in text
    assert "<BASE_B_MANUSCRIPT>" in text
    assert "# B" in text


def test_b_prime_retry_prompt_forbids_second_echo() -> None:
    prompt = build_b_prime_anti_echo_retry_prompt(
        package_md="source_id: S1\nclaim",
        base_b_manuscript="# B\n\n正文 [1]\n",
        humanizer_directive="humanize zh",
    )

    text = prompt.as_text()
    assert "逐字节相同" in text
    assert "至少执行 5 处局部修订" in text
    assert "不得 100% echo B 稿原文" in text
    assert "不要输出 self-critique 内容" in text
    assert "<BASE_B_MANUSCRIPT>" in text


def test_c_prompt_contract_has_no_evidence_package_or_artifacts() -> None:
    prompt = build_c_prompt(kernel=_kernel(), humanizer_directive="humanize zh")
    text = prompt.as_text()

    assert "公共物品理论" in text
    assert "不进行检索" in text
    assert "source_id" not in text
    assert "EVIDENCE_PACKAGE" not in text
    assert "合成 claims" in text


def test_g_prompt_combines_front_half_source_pool_with_ars_cut_point() -> None:
    prompt = build_g_ars_front_half_prompt(
        kernel=_kernel(),
        package_md="source_id: S1\nclaim",
        ars_full_mode_prompt="ARS full mode",
        humanizer_directive="humanize zh",
    )
    text = prompt.as_text()

    assert "source_id: S1" in text
    assert "ARS full mode" in text
    assert "front-half source pool" in text
    assert "ideator/drafter/stylist/final_rewrite/critic/integrity/export 全部砍掉" in text
    assert "不补充 evidence package 之外" in text
    assert "claim-to-source self-audit" in text
    assert "humanize zh" in text
