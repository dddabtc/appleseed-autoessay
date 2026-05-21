import { describe, expect, it, vi } from "vitest";

import { pickRetryStrategy, smartRetry } from "./retryStrategy";

describe("pickRetryStrategy (PR-I5: documentation only)", () => {
  it("routes worker-died-mid-flight classes to start", () => {
    expect(pickRetryStrategy("zombie_recovered")).toBe("start");
    expect(pickRetryStrategy("phase_runtime_error")).toBe("start");
  });

  it("routes graceful agent failures to rerun", () => {
    expect(pickRetryStrategy("failed_fixable")).toBe("rerun");
    expect(pickRetryStrategy("failed_vendor")).toBe("rerun");
    expect(pickRetryStrategy("failed_policy")).toBe("rerun");
  });

  it("defaults to start for unknown / null / empty class", () => {
    expect(pickRetryStrategy(null)).toBe("start");
    expect(pickRetryStrategy(undefined)).toBe("start");
    expect(pickRetryStrategy("")).toBe("start");
    expect(pickRetryStrategy("brand_new_unmapped_class")).toBe("start");
  });
});

describe("smartRetry (PR-I5: backend resolver call)", () => {
  it("calls POST /api/runs/{id}/phases/{phase}/retry", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            run_id: "r1",
            phase: "synthesizer",
            action: "start",
            expected_state: "SYNTHESIZER_RUNNING",
            job_id: "sync",
          }),
          {
            status: 202,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    const result = await smartRetry({
      runId: "r1",
      phase: "synthesizer",
      failureClass: "zombie_recovered",
    });
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/runs/r1/phases/synthesizer/retry",
      expect.objectContaining({ method: "POST" }),
    );
    expect(result.action).toBe("start");
    expect(result.expected_state).toBe("SYNTHESIZER_RUNNING");
    fetchSpy.mockRestore();
  });

  it("returns rerun action when backend picks rerun", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            run_id: "r1",
            phase: "synthesizer",
            action: "rerun",
            expected_state: "SYNTHESIZER_RUNNING",
            job_id: "sync",
          }),
          {
            status: 202,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    const result = await smartRetry({
      runId: "r1",
      phase: "synthesizer",
      failureClass: "failed_fixable",
    });
    expect(result.action).toBe("rerun");
    fetchSpy.mockRestore();
  });

  it("propagates backend 422 guidance_required without fallback", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "guidance_required",
              phase: "synthesizer",
              failure_class: "failed_fixable",
              guidance: "Upload more PDFs.",
            },
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    await expect(
      smartRetry({
        runId: "r1",
        phase: "synthesizer",
        failureClass: "failed_fixable",
      }),
    ).rejects.toThrow();
    // Crucially, fetch is called exactly once — PR-I5 dropped the
    // PR-I4.a 409-fallback because the backend now has full context.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    fetchSpy.mockRestore();
  });

  it("propagates backend 422 phase_mismatch without fallback", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              code: "phase_mismatch",
              requested_phase: "drafter",
              actual_failed_phase: "synthesizer",
            },
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    await expect(
      smartRetry({
        runId: "r1",
        phase: "drafter",
        failureClass: "zombie_recovered",
      }),
    ).rejects.toThrow();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    fetchSpy.mockRestore();
  });
});
