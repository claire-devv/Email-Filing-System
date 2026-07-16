from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from datetime import datetime

from PIL import Image
from pypdf import PdfReader

from app.services.classifier_service import ClassifierService
from app.services.filing_service import FilingService
from app.services.gmail_service import GmailService
from app.services.learning_service import LearningService
from app.services.pdf_service import PdfService
from app.services.types import ClassificationResult, EmailAttachment, EmailMessageData


def _image(path: Path, size: tuple[int, int]) -> None:
    image = Image.new("RGB", size, color=(240, 240, 240))
    image.save(path)


def main() -> None:
    gmail = GmailService()
    pdf = PdfService()
    cid_reference = gmail._classify_part(
        filename="image003.jpg",
        mime_type="image/jpeg",
        size_bytes=900_000,
        content_id="<image003.jpg@01DB>",
        disposition="attachment",
        body_html='<img src="cid:image003.jpg%4001DB">',
        dimensions=(2400, 1200),
    )
    assert cid_reference.kind == "inline_asset"
    assert cid_reference.issue is None

    # A descriptively-named, document-sized cid image (e.g. an iPhone screenshot the sender
    # attached in Mail) is a real document, not decoration -> real_attachment, flagged
    # ambiguous so it routes to review and still files to its own category folder.
    cid_document_screenshot = gmail._classify_part(
        filename="Screenshot 2026-06-26 at 1.28.11 AM.png",
        mime_type="image/png",
        size_bytes=900_000,
        content_id="<screenshot@iphone>",
        disposition="inline",
        body_html='<img src="cid:screenshot@iphone">',
        dimensions=(1170, 2532),
    )
    assert cid_document_screenshot.kind == "real_attachment"
    assert cid_document_screenshot.issue == "ambiguous_image_part"

    explicit_attachment = gmail._classify_part(
        filename="property-photo.jpg",
        mime_type="image/jpeg",
        size_bytes=12_000,
        content_id=None,
        disposition="attachment",
        body_html=None,
        dimensions=(120, 80),
    )
    assert explicit_attachment.kind == "real_attachment"
    assert explicit_attachment.issue is None

    inline_disposition = gmail._classify_part(
        filename="logo.png",
        mime_type="image/png",
        size_bytes=250_000,
        content_id=None,
        disposition="inline",
        body_html=None,
        dimensions=(900, 300),
    )
    assert inline_disposition.kind == "inline_asset"

    signature_image = gmail._classify_part(
        filename="image003.jpg",
        mime_type="image/jpeg",
        size_bytes=2_666,
        content_id=None,
        disposition=None,
        body_html=None,
        dimensions=(177, 35),
    )
    assert signature_image.kind == "inline_asset"

    ambiguous_image = gmail._classify_part(
        filename="image004.jpg",
        mime_type="image/jpeg",
        size_bytes=150_000,
        content_id=None,
        disposition=None,
        body_html=None,
        dimensions=(640, 480),
    )
    assert ambiguous_image.kind == "real_attachment"
    assert ambiguous_image.issue == "ambiguous_image_part"

    non_image = gmail._classify_part(
        filename="JSCD 761.pdf",
        mime_type="application/pdf",
        size_bytes=11_700_000,
        content_id=None,
        disposition=None,
        body_html=None,
    )
    assert non_image.kind == "real_attachment"

    # Gmail/Outlook forward: inline logo keeps a Content-ID but loses the HTML cid
    # reference and arrives with disposition=attachment. Signature-like -> inline.
    forwarded_inline_logo = gmail._classify_part(
        filename="image001.png",
        mime_type="image/png",
        size_bytes=6_276,
        content_id="<ii_19e998b354fad7999132>",
        disposition='attachment; filename="image001.png"',
        body_html=None,
        dimensions=(150, 43),
    )
    assert forwarded_inline_logo.kind == "inline_asset"

    # Microsoft Word temporary embedded-image artifact -> never a real attachment.
    word_temp_image = gmail._classify_part(
        filename="~WRD2182.jpg",
        mime_type="image/jpeg",
        size_bytes=823,
        content_id="<ii_19e998b354fdb45c7b31>",
        disposition='attachment; filename="~WRD2182.jpg"',
        body_html=None,
        dimensions=(100, 100),
    )
    assert word_temp_image.kind == "inline_asset"

    # A genuine small image attachment with NO Content-ID stays a real attachment.
    real_small_attachment = gmail._classify_part(
        filename="property-photo.jpg",
        mime_type="image/jpeg",
        size_bytes=12_000,
        content_id=None,
        disposition="attachment",
        body_html=None,
        dimensions=(120, 80),
    )
    assert real_small_attachment.kind == "real_attachment"

    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        ambiguous_path = tmp_path / "image004.jpg"
        _image(ambiguous_path, (640, 480))
        ambiguous_email = EmailMessageData(
            gmail_message_id="ambiguous",
            thread_id=None,
            sender="sender@example.com",
            recipient="file@rockreservices.com",
            subject="Ambiguous",
            received_at=None,
            body_text="Attached image.",
            body_html=None,
            attachments=[
                EmailAttachment(
                    filename="image004.jpg",
                    mime_type="image/jpeg",
                    local_path=ambiguous_path,
                    size_bytes=ambiguous_path.stat().st_size,
                    part_classification_reason="Generic image part has no decisive inline signal; retained for review.",
                    part_classification_issue="ambiguous_image_part",
                )
            ],
        )
        prepared_ambiguous = pdf.prepare_email(ambiguous_email, tmp_path / "ambiguous_work")
        assert "image004.jpg: ambiguous_image_part" in prepared_ambiguous.issues
        assert any(item.kind == "attachment" and item.original_filename == "image004.jpg" for item in prepared_ambiguous.artifacts)
        assert any(item.kind == "combined_package" for item in prepared_ambiguous.artifacts)
        body_reader = PdfReader(str(prepared_ambiguous.email_body_pdf))
        body_text = "\n".join((page.extract_text() or "") for page in body_reader.pages)
        # Gmail-print-style layout: subject title, sender, To line, body, attachment manifest.
        assert "Ambiguous" in body_text
        assert "sender@example.com" in body_text
        assert "To: file@rockreservices.com" in body_text
        assert "Attached image." in body_text
        assert "1 attachment" in body_text
        assert "image004.jpg" in body_text
        attachment_artifact = next(item for item in prepared_ambiguous.artifacts if item.kind == "attachment")
        cover_text = PdfReader(str(attachment_artifact.generated_pdf_path)).pages[0].extract_text() or ""
        assert "Attachment Source Note" in cover_text
        assert "Original Attachment" in cover_text
        # Combined package uses a slim divider, not the full per-attachment source note.
        combined_artifact = next(item for item in prepared_ambiguous.artifacts if item.kind == "combined_package")
        combined_text = "\n".join(
            (page.extract_text() or "") for page in PdfReader(str(combined_artifact.generated_pdf_path)).pages
        )
        assert "Attachment 1 of 1" in combined_text
        assert "Attachment Source Note" not in combined_text

    filing = FilingService()
    original_upload_combined = filing.drive.settings.upload_combined_package
    try:
        combined = SimpleNamespace(kind="combined_package", drive_file_id="old", drive_link="old", drive_folder_id="old", status="filed")
        attachment = SimpleNamespace(kind="attachment")
        email_body = SimpleNamespace(kind="email_body")
        # Client-facing output uploads the combined email package plus attachments.
        assert not filing._should_upload_artifact(email_body)
        assert filing._should_upload_artifact(attachment)
        filing.drive.settings.upload_combined_package = False
        assert not filing._should_upload_artifact(combined)
        filing._mark_internal(combined)
        assert combined.status == "internal"
        assert combined.drive_file_id is None
        assert combined.drive_link is None
        assert combined.drive_folder_id is None
        filing.drive.settings.upload_combined_package = True
        assert filing._should_upload_artifact(combined)
    finally:
        filing.drive.settings.upload_combined_package = original_upload_combined

    parsed = ClassifierService()._parse_json_response(
        """
        {
          "action": "file",
          "entity": "J. Claffey - JSCD 761 S. Cleveland LLC",
          "level2": "Insurance",
          "level3": null,
          "file_summary": "Gotham General Liability Policy for JSCD 761 S. Cleveland LLC",
          "artifact_summaries": {
            "email_body": "Gotham General Liability Policy Email for JSCD 761 S. Cleveland LLC",
            "JSCD 761.pdf": "Gotham General Liability Policy GL202600042494 for JSCD 761 S. Cleveland LLC"
          },
          "document_date": "2026-06-05",
          "confidence": 92,
          "unknown_entity": false,
          "needs_review_reason": null,
          "reason": "Policy attachment identifies Gotham GL policy."
        }
        """
    )
    assert parsed.decision_audit["artifact_summaries"]["JSCD 761.pdf"].startswith("Gotham General Liability Policy")

    naming_decision = ClassificationResult(
        entity="J. Claffey - JSCD 761 S. Cleveland LLC",
        level2="Insurance",
        level3=None,
        file_summary="Gotham General Liability Policy for JSCD 761 S. Cleveland LLC",
        confidence=92,
        unknown_entity=False,
        needs_review=False,
        reason="test",
        action="file",
        decision_audit={
            "filename_date": "2026.06.05",
            "artifact_summaries": parsed.decision_audit["artifact_summaries"],
            "email_sender": "John Claffey",
        },
    )
    email_stub = SimpleNamespace(
        received_at=datetime(2026, 6, 5),
        sender="John Claffey <jclaffey@example.com>",
        subject="Re: Gotham GL Policy",
    )
    body_name = filing._filename(email_stub, naming_decision, SimpleNamespace(kind="email_body", original_filename="email_body.pdf"))
    package_name = filing._filename(email_stub, naming_decision, SimpleNamespace(kind="combined_package", original_filename="combined_email_package.pdf"))
    attachment_name = filing._filename(email_stub, naming_decision, SimpleNamespace(kind="attachment", original_filename="JSCD 761.pdf"))
    assert body_name == "2026.06.05 - Email Regarding Gotham General Liability Policy Email for JSCD 761 S. Cleveland LLC.pdf"
    assert package_name == "2026.06.05 - John Claffey - Gotham GL Policy.pdf"
    assert attachment_name == "2026.06.05 - Gotham General Liability Policy GL202600042494 for JSCD 761 S. Cleveland LLC.pdf"

    # Free-mail senders learn the exact address + keywords but never a domain mapping;
    # organization domains still do.
    learning = LearningService()
    recorded: list[tuple[str, str]] = []
    learning._upsert = lambda db, pattern_type, pattern_value, *args, **kwargs: recorded.append((pattern_type, pattern_value))
    learning.record_sender_mapping(None, "ataurrehman3636@gmail.com", "E", "Property Taxes", None, "review_approve")
    assert ("sender", "ataurrehman3636@gmail.com") in recorded
    assert ("domain", "gmail.com") not in recorded
    recorded.clear()
    learning.record_sender_mapping(None, "jhoffman@ginsgroup.com", "E", "Insurance", None, "review_approve")
    assert ("domain", "ginsgroup.com") in recorded

    # Forwarder relay inboxes (they forward mail for every client) never learn
    # sender or domain mappings.
    from app.core.config import get_settings

    settings = get_settings()
    original_forwarders = settings.forwarder_domains
    settings.forwarder_domains = ["rockreservices.com"]
    try:
        recorded.clear()
        learning.record_sender_mapping(None, "Matthew Rodrigue <mpr@rockreservices.com>", "E", "Insurance", None, "review_correct")
        assert recorded == []
    finally:
        settings.forwarder_domains = original_forwarders

    classifier = ClassifierService()
    foremost_email = EmailMessageData(
        gmail_message_id="foremost",
        thread_id=None,
        sender="RRES File Cabinet <file@rockreservices.com>",
        recipient="file@rockreservices.com",
        cc=None,
        subject="Fwd: Foremost Professional Plaza - Financials",
        received_at=datetime(2026, 6, 10),
        body_text=(
            "From: Matthew Rodrigue <mpr@rockreservices.com>\n"
            "To: Mollie MacLeod <M.MacLeod@pacificacompanies.com>\n"
            "Subject: Re: Foremost Professional Plaza - Financials\n\n"
            "Hi Mollie,\nPlease see attached for May 2026.\n\n"
            "On Tue, Mar 10, 2026 Mollie MacLeod wrote:\n"
            "Would you please provide February financials for the Foremost Plaza property?"
        ),
        body_html=None,
    )
    prepared_foremost = SimpleNamespace(email=foremost_email, text_preview=foremost_email.body_text, artifacts=[], issues=[])
    original_forwarders = settings.forwarder_domains
    settings.forwarder_domains = ["rockreservices.com"]
    try:
        contacts = classifier._contact_hints(prepared_foremost)
        phrases = classifier._entity_phrase_hints(prepared_foremost)
        prompt = classifier._prompt(prepared_foremost, [], [])
        mollie = next(item for item in contacts if item["name"] == "Mollie MacLeod")
        matthew = next(item for item in contacts if item["name"] == "Matthew Rodrigue")
        assert mollie["principal_candidate"] is True
        assert mollie["formatted_name_if_used"] == "M. MacLeod"
        assert matthew["principal_candidate"] is False
        assert "Foremost Professional Plaza" in phrases
        assert '"F. Last - Entity Name"' in prompt
        assert "Mollie MacLeod" in prompt
        assert "M. MacLeod" in prompt
        assert "Foremost Professional Plaza" in prompt
        # Section-budgeted assembly: the email thread text survives and the whole prompt
        # stays within the configured cap.
        assert "provide February financials" in prompt
        assert len(prompt) <= settings.claude_max_prompt_chars
    finally:
        settings.forwarder_domains = original_forwarders

    # Entity handling at scale: every entity NAME is always shown (full recall), the heavier
    # aliases/properties are scoped to the relevant subset, and the email thread is never starved.
    long_thread = "Insurance renewal for Beacon Plaza LLC.\n" + ("filler line about the policy. " * 400)
    big_email = EmailMessageData(
        gmail_message_id="big", thread_id=None, sender="agent@example.com", recipient="file@rockreservices.com",
        cc=None, subject="Insurance for Beacon Plaza LLC", received_at=datetime(2026, 6, 10),
        body_text=long_thread, body_html=None,
    )
    prepared_big = SimpleNamespace(email=big_email, text_preview=long_thread, artifacts=[], issues=[])
    big_registry = [SimpleNamespace(entity_name=f"A. Person {i} - Entity {i} LLC", aliases=[f"alias-{i}"], properties=[]) for i in range(120)]
    big_registry.append(SimpleNamespace(entity_name="B. Owner - Beacon Plaza LLC", aliases=["Beacon Plaza LLC"], properties=[]))

    prompt_big = classifier._prompt(prepared_big, big_registry, [])
    # Full recall: every entity NAME is present -- both the relevant one and one far beyond
    # the detail subset -- so Claude can always match the correct entity.
    assert "B. Owner - Beacon Plaza LLC" in prompt_big
    assert "A. Person 119 - Entity 119 LLC" in prompt_big
    # Detail scoping: the relevant entity's alias is included (it ranks first); an entity
    # beyond the detail top-N has its alias scoped out (but its name is still listed above).
    assert "Beacon Plaza LLC" in prompt_big
    assert "alias-119" not in prompt_big
    # Thread is not starved by the registry, and the whole prompt stays within the cap.
    assert "Insurance renewal for Beacon Plaza" in prompt_big
    assert len(prompt_big) <= settings.claude_max_prompt_chars

    print({"inline_filter": "ok", "html_prepare": "ok", "free_mail_domain": "ok", "forwarder_guard": "ok", "entity_format_prompt": "ok", "prompt_budget": "ok", "entity_scaling": "ok"})


if __name__ == "__main__":
    main()
