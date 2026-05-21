from io import BytesIO

import httpx
import pytest
import respx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from autoessay.clients.pdf_text import PoorExtraction, extract_text
from autoessay.config import get_settings


@respx.mock
async def test_extract_text_reads_synthetic_pdf_bytes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AUTOESSAY_SYNTHESIZER_STUB", raising=False)
    get_settings.cache_clear()
    payload = _synthetic_pdf_bytes(" ".join(["extractable financial history text"] * 12))
    respx.get("https://example.test/synthetic.pdf").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=payload,
        ),
    )

    async with httpx.AsyncClient() as client:
        response = await client.get("https://example.test/synthetic.pdf")

    text = extract_text(response.content)

    assert len(text) >= 200
    assert "extractable financial history text" in text


def test_extract_text_raises_poor_extraction_on_too_short(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("AUTOESSAY_SYNTHESIZER_STUB", raising=False)
    get_settings.cache_clear()
    payload = _synthetic_pdf_bytes("too short")

    with pytest.raises(PoorExtraction, match="fewer than 200 characters"):
        extract_text(payload)


def _synthetic_pdf_bytes(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        },
    )
    font_ref = writer._add_object(font)
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})},
    )
    page[NameObject("/Resources")] = resources

    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)

    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
