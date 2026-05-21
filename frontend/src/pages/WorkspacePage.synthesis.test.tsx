import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SynthesisSubview } from "./WorkspacePage";

describe("SynthesisSubview source-review gate", () => {
  it("does not expose a bare synthesizer start path before deep-dive review is saved", () => {
    const html = renderToString(
      <SynthesisSubview
        runId="run-1"
        currentState="USER_DEEP_DIVE_REVIEW"
        paperMode="case_analysis"
        synthesisBundle={null}
        progress={[]}
        isStartingSynthesizer={false}
        isStartingFrameworkLens={false}
        isStartingIdeator={false}
        onRunSynthesizer={async () => undefined}
        onRunFrameworkLens={async () => undefined}
        onRunIdeator={async () => undefined}
        curatorCompleted={true}
        synthesizerCompleted={false}
      />,
    );

    expect(html).toContain('data-testid="phase-action-synthesizer"');
    expect(html).toContain("disabled=");
    expect(html).toContain('data-testid="synthesizer-disabled-hint"');
    expect(html).toContain("Sources tab");
  });
});
