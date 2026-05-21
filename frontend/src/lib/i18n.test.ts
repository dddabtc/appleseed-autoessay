import { describe, expect, it } from "vitest";

import { t } from "./i18n";

describe("research-kernel suggestion i18n", () => {
  it("has English and Chinese button copy", () => {
    expect(t("newrun.kernel.suggest_button", "en")).toBe("AI fill");
    expect(t("newrun.kernel.suggest_button", "zh")).toBe("AI 帮我填");
  });

  it("has English and Chinese failure copy", () => {
    expect(t("newrun.kernel.suggest_error_generic", "en")).toContain(
      "suggestion failed",
    );
    expect(t("newrun.kernel.suggest_error_generic", "zh")).toContain(
      "生成失败",
    );
  });
});
