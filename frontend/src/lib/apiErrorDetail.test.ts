/**
 * PR-385 regression: ``[object Object]`` shown to the user on
 * ``POST /api/projects`` 503. Root cause = ``String(detail)`` when
 * ``detail`` was a dict (FastAPI HTTPException(detail={...})). The
 * renderer now handles strings, arrays (pydantic 422), and structured
 * dicts (handler-raised HTTPException).
 */
import { describe, expect, it } from "vitest";

import { renderApiErrorDetail } from "./api";

describe("renderApiErrorDetail", () => {
  it("uses string detail verbatim", () => {
    expect(renderApiErrorDetail({ detail: "not authenticated" }, 401)).toBe(
      "not authenticated",
    );
  });

  it("renders dict detail via user_facing_reason", () => {
    const body = {
      detail: {
        code: "safety_gate_unavailable",
        user_facing_reason:
          "Safety check is temporarily unavailable. Please try again.",
        context_hint: "project.title",
      },
    };
    expect(renderApiErrorDetail(body, 503)).toBe(
      "Safety check is temporarily unavailable. Please try again.",
    );
  });

  it("renders pydantic 422 array as loc.path: msg pairs", () => {
    const body = {
      detail: [
        {
          loc: ["body", "title"],
          msg: "field required",
          type: "value_error.missing",
        },
        {
          loc: ["body", "domain_id"],
          msg: "field required",
          type: "value_error.missing",
        },
      ],
    };
    expect(renderApiErrorDetail(body, 422)).toBe(
      "body.title: field required; body.domain_id: field required",
    );
  });

  it("falls back to top-level error/message/reason fields when no detail", () => {
    expect(renderApiErrorDetail({ error: "boom" }, 500)).toBe("boom");
    expect(renderApiErrorDetail({ message: "boom2" }, 500)).toBe("boom2");
    expect(renderApiErrorDetail({ reason: "boom3" }, 500)).toBe("boom3");
  });

  it("falls back to status text when nothing usable", () => {
    expect(renderApiErrorDetail({}, 500)).toBe("Request failed: 500");
    expect(renderApiErrorDetail(null, 502)).toBe("Request failed: 502");
    expect(renderApiErrorDetail("", 504)).toBe("Request failed: 504");
  });

  it("returns string body directly", () => {
    expect(renderApiErrorDetail("plain text error", 500)).toBe(
      "plain text error",
    );
  });

  it("JSON.stringify dict detail when no user_facing_reason / message", () => {
    const body = { detail: { code: "x", note: "y" } };
    const out = renderApiErrorDetail(body, 500);
    expect(out).toContain("\"code\":\"x\"");
    expect(out).toContain("\"note\":\"y\"");
  });

  it("never returns the literal '[object Object]'", () => {
    const cases: unknown[] = [
      { detail: { foo: "bar" } },
      { detail: [{ loc: ["x"], msg: "y" }] },
      { detail: "literal" },
      { error: "oops" },
      {},
      null,
    ];
    for (const body of cases) {
      expect(renderApiErrorDetail(body, 500)).not.toContain("[object Object]");
    }
  });
});
