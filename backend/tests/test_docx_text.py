from io import BytesIO

import pytest
from docx import Document

from autoessay.clients.docx_text import extract_text
from autoessay.clients.pdf_text import PoorExtraction


def test_docx_extract_text_reads_paragraphs() -> None:
    payload = _docx_bytes(
        "Institutional archives can show how committees narrated credit risk. " * 5,
    )

    text = extract_text(payload, source_id="docx-test")

    assert "Institutional archives" in text
    assert len(text) >= 200


def test_docx_extract_text_rejects_too_short_document() -> None:
    payload = _docx_bytes("Too short.")

    with pytest.raises(PoorExtraction):
        extract_text(payload, source_id="short-docx")


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()
