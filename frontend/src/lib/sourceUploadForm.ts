export type SourceUploadTarget = {
  source_id: string;
  title: string;
  authors?: string[] | string | null;
  year?: number | null;
  doi?: string | null;
  url?: string | null;
  suggested_filename?: string | null;
};

export function buildSourceUploadFormData(
  target: SourceUploadTarget,
  pdf: File,
): FormData {
  const formData = new FormData();
  formData.set("source_id", target.source_id);
  formData.set("title", target.title);
  formData.set("pdf", pdf);
  const authors = Array.isArray(target.authors)
    ? target.authors.join(", ")
    : (target.authors ?? "");
  appendOptional(formData, "authors", authors);
  appendOptional(formData, "year", target.year?.toString() ?? "");
  appendOptional(formData, "doi", target.doi ?? "");
  appendOptional(formData, "url", target.url ?? "");
  appendOptional(formData, "suggested_filename", target.suggested_filename ?? "");
  return formData;
}

export function suggestedPdfFilename(sourceId: string): string {
  const cleaned = sourceId
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 96);
  return `${cleaned || "source"}.pdf`;
}

function appendOptional(formData: FormData, key: string, value: string): void {
  const trimmed = value.trim();
  if (trimmed) {
    formData.set(key, trimmed);
  }
}
