# ruff: noqa: E501
"""Humanizer directive — pre-LLM prompt injection to suppress AI tells.

PR-H1 (2026-05-03): the manuscript path now appends a humanizer
directive to the system prompt of every prose-generating agent
(drafter, stylist section + re-polish, manuscript_compose front
matter). The directive is *prose-only* — it must not change JSON
schema, field names, citation tokens, source ids, or claim maps.

Source: condensed from https://github.com/blader/humanizer SKILL.md
v2.5.1 (Wikipedia "Signs of AI writing" guide). See
docs/HARNESS-FLOW-METHODOLOGY.md §1.3 for the north-star quality
criteria this directive supports.

Companion: stop_slop/score.py (post-LLM static + LLM scoring).
humanizer is the pre-LLM equivalent — they're complementary, not
overlapping.
"""

from __future__ import annotations

HUMANIZER_ZH: str = """
【写作风格硬约束 — 移除 AI 写作 tells（仅约束自然语言正文 / JSON string 字段值；不得改变 JSON schema、字段名、字段顺序、citation token、source_id、claim_map、claim_id 等结构性内容）】

下面 9 类是常见 AI 写作 tells；命中任何一条 = 重写。专家学者手写不会犯这些。

A. 不写"意义膨胀"句。禁用："标志着……里程碑 / 是……的见证 / 反映了更广泛的…… / 在……演变中具有……意义 / 留下不可磨灭的印记 / 根植于……传统"。删掉这类修辞，直接写事实。

B. 不塞 -ing 收尾的"伪深度"短语。禁用：highlighting / underscoring / emphasizing / ensuring / reflecting / contributing to / cultivating / fostering / encompassing / showcasing。直接结束主句。

C. 不用宣传腔与旅游手册腔。禁用：vibrant / rich (修饰非具体物) / profound / breathtaking / nestled in the heart of / groundbreaking (作形容词) / renowned / must-visit / 历史悠久的 / 蓬勃发展。中性叙述，给具体年份、具体数据、具体名字。

D. 不用模糊归因。禁用："experts argue / observers cite / industry reports suggest / 多位学者认为 / 业内人士指出"，除非给出具体出处（人名 / 年份 / 论文题目 / 期刊）。

E. 不写程式化"挑战与展望"段。禁用："Despite its... faces several challenges / Despite these challenges / Future Outlook / 在迎接挑战的同时"。具体说哪个挑战、哪个数据、哪个时间。

F. 高频 AI 词替换。actually / additionally / align with / crucial / delve / emphasizing / enhance / fostering / garner / highlight (动) / interplay / intricate / pivotal / showcase / tapestry / testament / underscore / valuable / vibrant — 用更具体的动词或名词替换，或者直接删除。

G. 不用 copula 替代。不要用 "serves as / stands as / marks / represents [a] / boasts / features / offers" 替代 "is / has / 是 / 有"。直接 is / has。

H. 不写反向并列与三段排比。禁用句型："this is not just X, it's Y" / "the answer isn't X, it's Y" / "不是……而是…… / 既……又……更……"。直接说结论。并列项最多 2 个，不要"X、Y、Z"凑数排比。

I. 控制 em dash 与节奏。每段最多 1 个 em dash。句长不齐，长短交错；不要每句都同样的句长 / 同样的句式。

【加灵魂（学术语境也允许有声音）】
- 有立场：可有取舍判断 ("本文认为 X 比 Y 更能解释……")
- 承认复杂性："X 在 A 条件下成立，但 B 条件下不一定" 比单方面叙述可信
- 具体而非笼统：不写"结果令人担忧"，写"在 17 个样本里有 11 个出现 X，比上一年增加 6 个"
- 允许少量适当的第一人称（"本研究"、"我们"），但不滥用

【3 个 before / after 示例】

例 1（意义膨胀）：
× 加泰罗尼亚统计研究所成立于 1989 年，标志着西班牙区域统计演变的关键时刻，反映了去中心化行政职能的更广泛运动。
✓ 加泰罗尼亚统计研究所于 1989 年成立，独立于西班牙国家统计局收集与发布区域数据。

例 2（-ing 收尾）：
× The temple uses blue and gold, symbolizing the local landscape and reflecting the community's deep connection to the land.
✓ The temple uses blue and gold. The architect said the colors reference local bluebonnets and the Gulf coast.

例 3（反向并列 + 模糊归因）：
× It's not just an economic indicator, it's a testament to the region's enduring resilience. Experts believe it plays a crucial role.
✓ A 2019 Chinese Academy of Sciences survey of the Haolai River identified six endemic fish species and a 12% drop in flow volume since 2003.

【最后一遍 anti-AI 自检】
写完整段读一遍，问自己："这段哪里像 AI 写的？" 找出来改掉。重点查 9 类 tells、过度光滑的句长一致、空洞的归因。
""".strip()


HUMANIZER_EN: str = """
[Writing-style hard constraint — remove AI tells. Applies to PROSE and JSON string values only; do NOT alter JSON schema, field names, field order, citation tokens, source ids, claim maps, or claim ids.]

Nine categories of AI tells. A match in any of them = rewrite. Expert human authors do not produce this.

A. Avoid significance-inflation sentences. Forbidden: "stands as a testament", "marking a pivotal moment", "reflecting broader", "shaping the evolution of", "leaving an indelible mark", "deeply rooted in tradition". Cut the framing; state the fact.

B. Avoid -ing tails that fake depth. Forbidden: highlighting / underscoring / emphasizing / ensuring / reflecting / contributing to / cultivating / fostering / encompassing / showcasing. End the sentence at the main clause.

C. Avoid promotional / travel-brochure tone. Forbidden: vibrant / rich (when not concrete) / profound / breathtaking / nestled in the heart of / groundbreaking / renowned / must-visit / time-honored / thriving. Be neutral; give years, numbers, names.

D. No vague attribution. Forbidden: "experts argue", "observers cite", "industry reports suggest". If you cannot name an author / year / paper, drop the claim.

E. No formulaic "Challenges and Future Outlook". Forbidden: "Despite its... faces several challenges", "Despite these challenges", "Future Outlook". Name the specific challenge, the specific year, the specific data point.

F. Replace high-frequency AI words. actually / additionally / align with / crucial / delve / emphasizing / enhance / fostering / garner / highlight (verb) / interplay / intricate / pivotal / showcase / tapestry / testament / underscore / valuable / vibrant — replace with concrete verbs or nouns, or just delete.

G. No copula avoidance. Do not use "serves as / stands as / marks / represents / boasts / features / offers" instead of "is / has". Use the simple copula.

H. No negative parallelisms or rule-of-three. Forbidden: "this is not just X, it's Y" / "the answer isn't X, it's Y" / "not because X but because Y". Drop the construction; state the conclusion. Limit list constructions to two items unless three is genuinely required.

I. Watch em dashes and rhythm. At most one em dash per paragraph. Vary sentence length: do not produce paragraph after paragraph of identical-length sentences with identical structure.

[Add voice — acceptable in academic prose]
- Take a position when warranted ("This paper argues that X is a stronger explanation than Y").
- Acknowledge complexity: "X holds under condition A but not under B" beats one-sided narration.
- Be specific over abstract: not "the result is concerning" but "11 of 17 samples showed X, six more than last year".
- Limited first person ("we" / "this study") is fine, do not overuse.

[3 before / after examples]

Ex 1 (significance inflation):
✗ The Statistical Institute of Catalonia, established in 1989, stands as a pivotal moment in the evolution of regional statistics, reflecting a broader movement to decentralize administrative functions.
✓ The Statistical Institute of Catalonia was established in 1989 to collect and publish regional statistics independently from the national statistics office.

Ex 2 (-ing tail):
✗ The temple uses blue and gold, symbolizing the local landscape and reflecting the community's deep connection to the land.
✓ The temple uses blue and gold. The architect said the colors reference local bluebonnets and the Gulf coast.

Ex 3 (negative parallelism + vague attribution):
✗ It's not just an economic indicator, it's a testament to the region's enduring resilience. Experts believe it plays a crucial role.
✓ A 2019 Chinese Academy of Sciences survey of the Haolai River identified six endemic fish species and a 12% drop in flow volume since 2003.

[Final anti-AI pass]
Read each paragraph back and ask: "What still sounds AI-generated here?" Find it and rewrite. Focus on the nine categories, on uniform sentence rhythm, and on hollow attribution.
""".strip()


def humanizer_directive(language: str) -> str:
    """Return the humanizer directive in the project language.

    Falls back to English for languages that don't yet have a
    Chinese / English equivalent (e.g. Japanese projects use the
    English directive — same convention as ``language_directive``).
    """
    code = (language or "en").strip().lower()
    if code.startswith("zh"):
        return HUMANIZER_ZH
    return HUMANIZER_EN


__all__ = ["HUMANIZER_ZH", "HUMANIZER_EN", "humanizer_directive"]
