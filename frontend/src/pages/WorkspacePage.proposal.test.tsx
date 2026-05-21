import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProposalSubview } from "./WorkspacePage";

function renderProposalSubview(
  currentState = "USER_SEARCH_REVIEW",
  proposalMissing = true,
): string {
  return renderToString(
    <ProposalSubview
      currentState={currentState}
      proposalBundle={null}
      proposalMissing={proposalMissing}
      progress={[]}
      isStartingProposal={false}
      isSavingProposal={false}
      isAcceptingProposal={false}
      onGenerate={async () => undefined}
      onRegenerate={async () => undefined}
      onSave={async () => undefined}
      onAccept={async () => undefined}
    />,
  );
}

describe("ProposalSubview proposal-less empty state", () => {
  it("renders a stable empty state for proposal-less runs past scout", () => {
    const html = renderProposalSubview();

    expect(html).toContain('data-testid="workspace-proposal-empty-state"');
    expect(html).toContain(
      'data-testid="workspace-proposal-empty-state-title"',
    );
    expect(html).toContain('data-testid="workspace-proposal-empty-state-body"');
    expect(html).toContain("No proposal yet");
  });

  it("keeps the initial proposal form at DOMAIN_LOADED", () => {
    const html = renderProposalSubview("DOMAIN_LOADED");

    expect(html).not.toContain('data-testid="workspace-proposal-empty-state"');
    expect(html).toContain("Generate Initial Proposal");
  });
});
