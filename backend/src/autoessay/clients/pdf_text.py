"""PDF text extraction for Synthesizer source notes."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

from autoessay.config import get_settings

MIN_EXTRACTED_CHARS = 200


class PoorExtraction(RuntimeError):
    """Raised when a PDF cannot yield enough machine-readable text."""


def extract_text(pdf_bytes: bytes, source_id: str | None = None) -> str:
    if get_settings().synthesizer_stub:
        source_label = source_id or "unknown"
        return (
            f"STUB-EXTRACTED-TEXT for source {source_label}\n\n"
            "Synthetic claim: the paper identifies a consensus pattern in the source base.\n"
            "Synthetic claim: the paper reports a finding about credit-market stress.\n"
            "Synthetic claim: the paper names a methodological limit for causal inference.\n"
        )

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - caller needs a deterministic warning class.
        raise PoorExtraction(f"PDF text extraction failed: {exc}") from exc

    extracted = "\f".join(page_texts)
    if len(extracted.strip()) < MIN_EXTRACTED_CHARS:
        raise PoorExtraction(
            f"PDF text extraction yielded fewer than {MIN_EXTRACTED_CHARS} characters",
        )
    return extracted
