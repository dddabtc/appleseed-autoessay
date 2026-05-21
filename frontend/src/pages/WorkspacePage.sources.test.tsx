import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type {
  DiscoverySource,
  FulltextManifestEntry,
  ManualUploadRequest,
} from "../lib/api";

import { ManualRequestsList, SourcesSubview } from "./WorkspacePage";

const source: DiscoverySource = {
  source_id: "crossref-fetch-failed",
  title: "The Log of Gravity",
  authors: ["Tinbergen"],
  year: 1962,
  venue: "Journal",
  doi: "10.1234/gravity",
  url: "https://example.test/gravity",
  pdf_url: "https://example.test/gravity.pdf",
  abstract: null,
  source_client: "crossref",
  access_status: "fetch_failed",
  license: null,
  rank_score: 1,
  risk_flags: [],
};

const metadataOnlySource: DiscoverySource = {
  ...source,
  source_id: "metadata-only",
  title: "Metadata-only article",
  access_status: "metadata_only",
  risk_flags: [],
};

function renderSources(
  manifest: Record<string, FulltextManifestEntry> = {},
  shortlist: DiscoverySource[] = [source],
): string {
  return renderToString(
    <SourcesSubview
      runId="run-1"
      currentState="USER_DEEP_DIVE_REVIEW"
      skimCandidates={[]}
      shortlist={shortlist}
      manifest={manifest}
      manualRequests={[]}
      curationReport=""
      sourceQualityCounts={{}}
      isStartingCurator={false}
      isStartingSynthesizer={false}
      isUploadingPdf={false}
      onRunCurator={async () => undefined}
      onRunSynthesizer={async () => undefined}
      onUploadPdf={async () => undefined}
      scoutCompleted={true}
      curatorCompleted={true}
      scoutProgress={[]}
      curatorProgress={[]}
    />,
  );
}

describe("SourcesSubview inline PDF upload", () => {
  it("defaults USER_SEARCH_REVIEW to sorted Scout candidates when shortlist is empty", () => {
    const lowScore = {
      ...source,
      source_id: "low-score",
      title: "Low score candidate",
      rank_score: 1,
    };
    const highScore = {
      ...source,
      source_id: "high-score",
      title: "High score candidate",
      rank_score: 9,
    };
    const html = renderToString(
      <SourcesSubview
        runId="run-1"
        currentState="USER_SEARCH_REVIEW"
        skimCandidates={[lowScore, highScore]}
        shortlist={[]}
        manifest={{}}
        manualRequests={[]}
        curationReport=""
        sourceQualityCounts={{}}
        isStartingCurator={false}
        isStartingSynthesizer={false}
        isUploadingPdf={false}
        onRunCurator={async () => undefined}
        onRunSynthesizer={async () => undefined}
        onUploadPdf={async () => undefined}
        scoutCompleted={true}
        curatorCompleted={false}
        scoutProgress={[]}
        curatorProgress={[]}
      />,
    );

    expect(html).toContain(
      'data-testid="workspace-sources-tab-skimmed" data-active="true"',
    );
    expect(html).toContain(
      'data-testid="workspace-sources-scout-candidates-notice"',
    );
    expect(html.indexOf("High score candidate")).toBeLessThan(
      html.indexOf("Low score candidate"),
    );
  });

  it("explains that Scout candidates are not a curated shortlist yet", () => {
    const html = renderToString(
      <SourcesSubview
        runId="run-1"
        currentState="USER_SEARCH_REVIEW"
        skimCandidates={[source]}
        shortlist={[]}
        manifest={{}}
        manualRequests={[]}
        curationReport=""
        sourceQualityCounts={{}}
        isStartingCurator={false}
        isStartingSynthesizer={false}
        isUploadingPdf={false}
        onRunCurator={async () => undefined}
        onRunSynthesizer={async () => undefined}
        onUploadPdf={async () => undefined}
        scoutCompleted={true}
        curatorCompleted={false}
        scoutProgress={[]}
        curatorProgress={[]}
      />,
    );

    expect(html).toContain("Scout candidates");
    expect(html).toContain("not been curated yet");
  });

  it("requires a source review decision before curation starts", () => {
    const html = renderToString(
      <SourcesSubview
        runId="run-1"
        currentState="USER_SEARCH_REVIEW"
        skimCandidates={[source]}
        shortlist={[]}
        manifest={{}}
        manualRequests={[]}
        curationReport=""
        sourceQualityCounts={{}}
        isStartingCurator={false}
        isStartingSynthesizer={false}
        isUploadingPdf={false}
        onRunCurator={async () => undefined}
        onRunSynthesizer={async () => undefined}
        onUploadPdf={async () => undefined}
        scoutCompleted={true}
        curatorCompleted={false}
        scoutProgress={[]}
        curatorProgress={[]}
      />,
    );

    expect(html).toContain('data-testid="workspace-source-review-panel"');
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-review-approved-button"',
    );
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-review-rejected-button"',
    );
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-review-pinned-button"',
    );
    expect(html).toContain("Approve or pin at least one Scout candidate");
    expect(html).toContain('data-testid="phase-action-curator"');
    expect(html).toContain("disabled=");
  });

  it("defaults deep-dive shortlist review to approved sources", () => {
    const html = renderSources();

    expect(html).toContain('data-testid="workspace-source-review-panel"');
    expect(html).toContain("1/1 selected");
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-review-approved-button"',
    );
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-review-status"',
    );
  });

  it("renders source quality diagnostics and weak-anchor badges", () => {
    const weakSource = {
      ...source,
      risk_flags: ["weak_entity_anchor"],
    };
    const html = renderToString(
      <SourcesSubview
        runId="run-1"
        currentState="USER_DEEP_DIVE_REVIEW"
        skimCandidates={[]}
        shortlist={[weakSource]}
        manifest={{}}
        manualRequests={[]}
        curationReport=""
        sourceQualityCounts={{
          off_topic_dropped: 3,
          verification_rejected: 2,
          runner_up: 1,
          weak_anchor: 1,
        }}
        isStartingCurator={false}
        isStartingSynthesizer={false}
        isUploadingPdf={false}
        onRunCurator={async () => undefined}
        onRunSynthesizer={async () => undefined}
        onUploadPdf={async () => undefined}
        scoutCompleted={true}
        curatorCompleted={true}
        scoutProgress={[]}
        curatorProgress={[]}
      />,
    );

    expect(html).toContain('data-testid="workspace-sources-quality-counts"');
    expect(html).toContain(
      'data-testid="workspace-sources-quality-count-off_topic_dropped"',
    );
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-weak-anchor-badge"',
    );
    expect(html).toContain("Weak topic anchor");
  });

  it("uses the blocked phase in the curator disabled hint", () => {
    const html = renderToString(
      <SourcesSubview
        runId="run-1"
        currentState="FAILED_POLICY"
        skimCandidates={[source]}
        shortlist={[]}
        manifest={{}}
        manualRequests={[]}
        curationReport=""
        sourceQualityCounts={{}}
        isStartingCurator={false}
        isStartingSynthesizer={false}
        isUploadingPdf={false}
        blockedPhase="exports"
        onRunCurator={async () => undefined}
        onRunSynthesizer={async () => undefined}
        onUploadPdf={async () => undefined}
        scoutCompleted={true}
        curatorCompleted={false}
        scoutProgress={[]}
        curatorProgress={[]}
      />,
    );

    expect(html).toContain(
      'data-testid="workspace-sources-curator-disabled-hint"',
    );
    expect(html).toContain("blocked in exports");
  });

  it("keeps the global upload button available", () => {
    const html = renderSources();

    expect(html).toContain('data-testid="sources-global-upload-pdf-button"');
  });

  it("renders an inline upload control for a source without a PDF", () => {
    const html = renderSources();

    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-upload-pdf-button"',
    );
    expect(html).toContain(
      'data-testid="source-row-crossref-fetch-failed-upload-pdf-input"',
    );
    expect(html).toContain("Upload PDF for The Log of Gravity");
    expect(html).toContain("crossref-fetch-failed.pdf");
  });

  it("does not show a bound upload control for metadata-only rows", () => {
    const html = renderSources({}, [metadataOnlySource]);

    expect(html).not.toContain(
      'data-testid="source-row-metadata-only-upload-pdf-button"',
    );
  });

  it("keeps existing PDF links instead of showing upload for fetched sources", () => {
    const html = renderSources({
      "crossref-fetch-failed": {
        pdf_path: "sources/crossref-fetch-failed.pdf",
        sha256: "abc",
        size_bytes: 4,
        fetched_at: "2026-05-11T00:00:00Z",
        license: null,
      },
    });

    expect(html).toContain("/api/runs/run-1/sources/crossref-fetch-failed/pdf");
    expect(html).not.toContain(
      'data-testid="source-row-crossref-fetch-failed-upload-pdf-button"',
    );
  });

  it("renders per-item upload controls for manual upload requests", () => {
    const request: ManualUploadRequest = {
      source_id: "rcep-literature",
      title: "RCEP literature",
      doi: null,
      url: "https://example.test/rcep",
      suggested_location: "sources/uploads/rcep-literature.pdf",
      reason: "Automatic PDF fetch failed.",
    };

    const html = renderToString(
      <ManualRequestsList
        requests={[request]}
        isUploadingPdf={false}
        onUploadPdf={async () => undefined}
      />,
    );

    expect(html).toContain(
      'data-testid="manual-request-rcep-literature-upload-pdf-button"',
    );
    expect(html).toContain(
      'data-testid="manual-request-rcep-literature-upload-pdf-input"',
    );
    expect(html).toContain("Upload PDF for RCEP literature");
    expect(html).toContain("sources/uploads/rcep-literature.pdf");
  });
});
