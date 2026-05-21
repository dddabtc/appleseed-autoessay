// PR-C0.b2.ui: shared helper to fill the research-kernel intake
// form on NewRunPage. PR-C0 made the kernel form a required gate,
// so every spec that creates a run must fill the minimal required
// fields before [data-testid="newrun-submit"] enables.
//
// Defaults: case_analysis (default mode, "available" status — no
// developer-preview ack needed) + just-enough text to clear the
// 30-char observed_puzzle / non-empty tentative_question / scope
// validators.
//
// 2026-05-06 multi-topic stability harness: pick a fixture by
// AUTOESSAY_E2E_KERNEL_ID env. Default keeps backward-compat with
// the original 江南刊本 fixture for any spec that hasn't been
// updated yet.
//
// 2026-05-07: paper_language is fixture-declared. zh fixtures
// generate Chinese papers (CNKI front+back matter wrapper +
// cite-marker normalize gate). en fixtures bypass both.
// Override per-run with AUTOESSAY_E2E_PAPER_LANGUAGE=zh|en|ja.

import { expect, type Page } from "@playwright/test";

const PREFIX = "newrun-kernel";

type PaperLanguage = "zh" | "en" | "ja";

interface KernelFixture {
  observed_puzzle: string;
  tentative_question: string;
  scope: string;
  paper_language: PaperLanguage;
}

const KERNEL_FIXTURES: Record<string, KernelFixture> = {
  jiangnan_publishing: {
    observed_puzzle:
      "既有研究在断代与文体归属上存在反复张力，需要重新检视一手材料以厘清边界。",
    tentative_question: "此组文献的断代依据如何被重新建立？",
    scope: "以 19 世纪后期江南刊本为限，仅含序跋与刻工题记。",
    paper_language: "zh",
  },
  bretton_woods: {
    observed_puzzle:
      "战后布雷顿森林体系的金本位安排在制度文本上长期保留，但实际可兑换约束在 1960 年代已被多重例外事实上掏空，既有研究对这一断裂的时点判断仍不一致。",
    tentative_question:
      "布雷顿森林金本位承诺的实际约束力，应当依据哪些档案性证据来重新断定其失效节点？",
    scope:
      "限定 1960-1971 年美元—黄金兑换通道，以 IMF 内部备忘录、美联储理事会会议纪要与黄金池 (London Gold Pool) 季度结算记录为主。",
    paper_language: "zh",
  },
  wang_yangming_turn: {
    observed_puzzle:
      "明末清初阳明心学在江南地区的传播路径与早期王门弟子的诠释取向之间存在显著差异，但既有思想史研究多把这一阶段视为一次性扩散，未充分区分讲会、刻书与官学制度三类传播渠道的不同作用。",
    tentative_question:
      "在明末清初江南语境中，阳明心学的传播究竟应被理解为一次性思想扩散，还是不同制度渠道并行竞争的多线过程？",
    scope:
      "限定 1573-1644 年江南地区，材料以王门弟子讲会语录、刊本序跋、府县学官档与同时期文集为主，不纳入清代以后的回顾性叙述。",
    paper_language: "zh",
  },
  fed_unconventional: {
    observed_puzzle:
      "2008 年金融危机后美联储的非常规货币政策（量化宽松、前瞻指引、定向再贷款）被广泛讨论，但既有评估对其金融稳定效应与通胀传导效应给出了相反结论，部分原因是不同研究使用了相互冲突的政策时窗划分。",
    tentative_question:
      "美联储 2008-2014 年非常规政策的真实政策时窗，应当依据哪些一手会议记录与资产负债表数据重新划分？",
    scope:
      "限定 2008-2014 年美联储 FOMC，材料以 FOMC 会议纪要 transcript（5 年延迟公开）、H.4.1 周度资产负债表与纽联储公开市场操作日志为主。",
    paper_language: "zh",
  },
};

export async function fillNewRunKernel(page: Page): Promise<void> {
  const kernelId = process.env.AUTOESSAY_E2E_KERNEL_ID ?? "jiangnan_publishing";
  const fixture = KERNEL_FIXTURES[kernelId];
  if (!fixture) {
    throw new Error(
      `unknown AUTOESSAY_E2E_KERNEL_ID=${kernelId}; available: ` +
        Object.keys(KERNEL_FIXTURES).join(", "),
    );
  }
  const overrideLang = process.env.AUTOESSAY_E2E_PAPER_LANGUAGE as
    | PaperLanguage
    | undefined;
  const paperLanguage = overrideLang ?? fixture.paper_language;
  console.log(`[kernel] using fixture: ${kernelId} (paper_language=${paperLanguage})`);
  await expect(page.locator(`[data-testid="${PREFIX}-form"]`)).toBeVisible({
    timeout: 30_000,
  });
  await page
    .locator('[data-testid="newrun-paper-language"]')
    .selectOption(paperLanguage);
  await page.locator(`[data-testid="${PREFIX}-observed-puzzle"]`).fill(fixture.observed_puzzle);
  await page
    .locator(`[data-testid="${PREFIX}-tentative-question"]`)
    .fill(fixture.tentative_question);
  await page.locator(`[data-testid="${PREFIX}-scope"]`).fill(fixture.scope);
  await page.locator(`[data-testid="${PREFIX}-primary-yes"]`).click();
}
