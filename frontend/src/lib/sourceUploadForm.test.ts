import { describe, expect, it } from "vitest";

import {
  buildSourceUploadFormData,
  suggestedPdfFilename,
} from "./sourceUploadForm";

describe("buildSourceUploadFormData", () => {
  it("associates an uploaded PDF with the selected source_id", () => {
    const pdf = new File([new Uint8Array([37, 80, 68, 70])], "paper.pdf", {
      type: "application/pdf",
    });

    const formData = buildSourceUploadFormData(
      {
        source_id: "crossref:10.1234/example",
        title: "The Log of Gravity",
        authors: ["Jane Author", "Q. Researcher"],
        year: 2024,
        doi: "10.1234/example",
        url: "https://example.test/paper",
        suggested_filename: "crossref_10.1234_example.pdf",
      },
      pdf,
    );

    expect(formData.get("source_id")).toBe("crossref:10.1234/example");
    expect(formData.get("title")).toBe("The Log of Gravity");
    expect(formData.get("authors")).toBe("Jane Author, Q. Researcher");
    expect(formData.get("year")).toBe("2024");
    expect(formData.get("doi")).toBe("10.1234/example");
    expect(formData.get("url")).toBe("https://example.test/paper");
    expect(formData.get("suggested_filename")).toBe(
      "crossref_10.1234_example.pdf",
    );
    expect(formData.get("pdf")).toBe(pdf);
  });

  it("omits blank optional metadata for manual requests", () => {
    const pdf = new File([new Uint8Array([37, 80, 68, 70])], "paper.pdf", {
      type: "application/pdf",
    });

    const formData = buildSourceUploadFormData(
      {
        source_id: "rcep-study",
        title: "RCEP literature",
        authors: [],
        year: null,
        doi: null,
        url: null,
      },
      pdf,
    );

    expect(formData.get("source_id")).toBe("rcep-study");
    expect(formData.get("title")).toBe("RCEP literature");
    expect(formData.has("authors")).toBe(false);
    expect(formData.has("year")).toBe(false);
    expect(formData.has("doi")).toBe(false);
    expect(formData.has("url")).toBe(false);
  });

  it("builds a safe filename suggestion from a source id", () => {
    expect(suggestedPdfFilename("crossref:10.1234/example")).toBe(
      "crossref_10.1234_example.pdf",
    );
    expect(suggestedPdfFilename("   ")).toBe("source.pdf");
  });
});
