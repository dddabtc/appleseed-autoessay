import { describe, expect, it } from "vitest";

import type { PhaseHistoryEntry, PhaseHistoryVersionEntry } from "./api";
import {
  deriveCardState,
  derivePrimaryActions,
  describeDeleteBlock,
  describeVersionSource,
  isActivateDisabled,
  isDeleteDisabled,
} from "./phaseHistoryState";

function entry(
  overrides: Partial<PhaseHistoryEntry> & {
    head_missing?: boolean;
    prompt_dirty?: boolean;
    lineage_dirty?: boolean;
  } = {},
): PhaseHistoryEntry {
  const {
    head_missing = false,
    prompt_dirty = false,
    lineage_dirty = false,
    ...rest
  } = overrides;
  return {
    phase: "synthesizer",
    state_flags: {
      head_missing,
      prompt_dirty,
      lineage_dirty,
    },
    head_pv_id: head_missing ? null : "pv_head",
    head_version_no: head_missing ? null : 1,
    upstream_summary: [],
    versions: [],
    runnable_now: false,
    ...rest,
  };
}

function version(
  overrides: Partial<PhaseHistoryVersionEntry> = {},
): PhaseHistoryVersionEntry {
  return {
    pv_id: "pv_test",
    version_no: 1,
    source: "agent",
    status: "done",
    created_at: "2026-05-02T00:00:00Z",
    is_head: false,
    upstream_lineage: [],
    has_downstream_dependents: false,
    dependent_summary: null,
    delete_blocked: false,
    delete_block_reason: null,
    ...overrides,
  };
}

describe("deriveCardState", () => {
  it("head_missing wins over every other flag", () => {
    expect(
      deriveCardState(
        entry({ head_missing: true, prompt_dirty: true, lineage_dirty: true }),
      ),
    ).toBe("ungenerated");
  });

  it("prompt_dirty wins over lineage_dirty (codex amendment 5)", () => {
    expect(
      deriveCardState(
        entry({ prompt_dirty: true, lineage_dirty: true }),
      ),
    ).toBe("prompt_edited");
  });

  it("lineage_dirty alone → upstream_superseded", () => {
    expect(deriveCardState(entry({ lineage_dirty: true }))).toBe(
      "upstream_superseded",
    );
  });

  it("no flags → generated", () => {
    expect(deriveCardState(entry({}))).toBe("generated");
  });
});

describe("derivePrimaryActions", () => {
  it("generated → rerun + edit_prompt", () => {
    expect(derivePrimaryActions(entry({}))).toEqual([
      "rerun",
      "edit_prompt",
    ]);
  });

  it("prompt_edited → cancel + regenerate", () => {
    expect(
      derivePrimaryActions(entry({ prompt_dirty: true })),
    ).toEqual(["cancel_prompt", "regenerate"]);
  });

  it("upstream_superseded → activate_lineage_match + rerun_for_new_match", () => {
    expect(
      derivePrimaryActions(entry({ lineage_dirty: true })),
    ).toEqual(["activate_lineage_match", "rerun_for_new_match"]);
  });

  it("ungenerated runnable_now=true with versions → run_now + scroll_to_versions", () => {
    expect(
      derivePrimaryActions(
        entry({
          head_missing: true,
          runnable_now: true,
          versions: [version()],
        }),
      ),
    ).toEqual(["run_now", "scroll_to_versions"]);
  });

  it("ungenerated runnable_now=false no versions → run_now (rendered disabled)", () => {
    // Round-2 audit (always-render + disabled + hint): run_now is
    // always emitted for ungenerated phases; the card disables and
    // shows a dependency hint when runnable_now=false.
    expect(
      derivePrimaryActions(
        entry({
          head_missing: true,
          runnable_now: false,
          versions: [],
        }),
      ),
    ).toEqual(["run_now"]);
  });

  it("ungenerated runnable_now=false with versions → run_now + scroll", () => {
    // Same rule: run_now is always present for ungenerated.
    expect(
      derivePrimaryActions(
        entry({
          head_missing: true,
          runnable_now: false,
          versions: [version()],
        }),
      ),
    ).toEqual(["run_now", "scroll_to_versions"]);
  });
});

describe("describeDeleteBlock", () => {
  it("not blocked → null", () => {
    expect(describeDeleteBlock(version())).toBeNull();
  });

  it("active_head reason", () => {
    expect(
      describeDeleteBlock(
        version({ delete_blocked: true, delete_block_reason: "active_head" }),
      ),
    ).toEqual({ key: "active_head", interpolation: {} });
  });

  it("lineage_child reason", () => {
    expect(
      describeDeleteBlock(
        version({
          delete_blocked: true,
          delete_block_reason: "lineage_child",
        }),
      ),
    ).toEqual({ key: "lineage_child", interpolation: {} });
  });

  it("fork_point parses branch name", () => {
    expect(
      describeDeleteBlock(
        version({
          delete_blocked: true,
          delete_block_reason: "fork_point:test-fork",
        }),
      ),
    ).toEqual({ key: "fork_point", interpolation: { branch: "test-fork" } });
  });

  it("downstream-dependent fallback", () => {
    expect(
      describeDeleteBlock(
        version({
          delete_blocked: true,
          delete_block_reason: "ideator v1",
        }),
      ),
    ).toEqual({
      key: "downstream_dependent",
      interpolation: { name: "ideator v1" },
    });
  });
});

describe("isActivateDisabled", () => {
  it("disabled when is_head=true", () => {
    expect(isActivateDisabled(version({ is_head: true }))).toBe(true);
  });

  it("disabled when status !== done", () => {
    expect(isActivateDisabled(version({ status: "failed" }))).toBe(true);
    expect(isActivateDisabled(version({ status: "running" }))).toBe(true);
  });

  it("enabled when not head + done", () => {
    expect(isActivateDisabled(version())).toBe(false);
  });
});

describe("isDeleteDisabled", () => {
  it("delegates to delete_blocked", () => {
    expect(isDeleteDisabled(version())).toBe(false);
    expect(isDeleteDisabled(version({ delete_blocked: true }))).toBe(true);
  });
});

describe("describeVersionSource", () => {
  it("agent → default", () => {
    expect(describeVersionSource("agent")).toBe("default");
  });

  it("user_edit → user_edit", () => {
    expect(describeVersionSource("user_edit")).toBe("user_edit");
  });

  it("unknown → other", () => {
    expect(describeVersionSource("user_replace")).toBe("other");
  });
});
