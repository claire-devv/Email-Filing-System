"""Guards for attachment-heavy / large-PDF emails: page-truncation + config wiring.

Run: python -m app.scripts.test_pdf_guards
"""
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.core.config import get_settings
from app.services.classifier_service import ClassifierService


def _make_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def main() -> None:
    svc = ClassifierService()
    max_pages = get_settings().claude_pdf_max_pages

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        # Over-limit PDF -> truncated to exactly max_pages, written as a new file.
        big = _make_pdf(d / "big.pdf", max_pages + 50)
        out = svc._pdf_for_claude(big)
        assert out is not None and out != big, "over-limit PDF should be truncated to a new path"
        assert len(PdfReader(str(out)).pages) == max_pages, len(PdfReader(str(out)).pages)

        # Under-limit PDF -> returned unchanged (no rewrite).
        small = _make_pdf(d / "small.pdf", 10)
        assert svc._pdf_for_claude(small) == small, "small PDF should be sent as-is"

        # Exactly at the limit -> unchanged.
        exact = _make_pdf(d / "exact.pdf", max_pages)
        assert svc._pdf_for_claude(exact) == exact

        # Unreadable file -> None (caller falls back to the text preview, no API error).
        bad = d / "bad.pdf"
        bad.write_bytes(b"not a real pdf")
        assert svc._pdf_for_claude(bad) is None, "unreadable PDF should return None"

    # Config wiring is live.
    s = get_settings()
    assert s.max_auto_split_entities == 4, s.max_auto_split_entities
    assert s.claude_pdf_total_max_mb >= 1
    assert s.claude_max_prompt_chars >= 120000, s.claude_max_prompt_chars
    assert s.claude_artifact_payload_chars >= 80000, s.claude_artifact_payload_chars

    print("pdf guards: all assertions passed")
    print({
        "max_pages": max_pages,
        "max_auto_split_entities": s.max_auto_split_entities,
        "claude_pdf_total_max_mb": s.claude_pdf_total_max_mb,
        "claude_max_prompt_chars": s.claude_max_prompt_chars,
        "claude_artifact_payload_chars": s.claude_artifact_payload_chars,
    })


if __name__ == "__main__":
    main()
