from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class EmailAttachment:
    filename: str
    mime_type: str | None
    local_path: Path
    size_bytes: int
    content_id: str | None = None
    part_classification_reason: str | None = None
    part_classification_issue: str | None = None


@dataclass
class InlineAsset:
    filename: str
    mime_type: str | None
    local_path: Path
    size_bytes: int
    content_id: str | None = None
    width: int | None = None
    height: int | None = None
    part_classification_reason: str | None = None


@dataclass
class EmailMessageData:
    gmail_message_id: str
    thread_id: str | None
    sender: str | None
    recipient: str | None
    subject: str | None
    received_at: datetime | None
    body_text: str
    body_html: str | None
    cc: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)
    inline_assets: list[InlineAsset] = field(default_factory=list)
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class PreparedArtifact:
    kind: str
    original_filename: str | None
    local_path: Path
    generated_pdf_path: Path | None
    mime_type: str | None
    text_preview: str
    file_hash: str | None = None
    size_bytes: int | None = None
    requires_claude_pdf: bool = False
    # Page count, computed once during prep, so the classifier doesn't re-parse the PDF to decide
    # whether it needs truncating before sending to Claude.
    page_count: int | None = None
    issue: str | None = None
    # The Gmail part-classifier suspects this image is decoration (signature logo/banner) but kept
    # it as a real attachment out of caution. NOT stored in `issue` -- that field means
    # "unsupported/failed" and would stop the file from being classified at all. Claude confirms or
    # denies via the per-attachment "decorative" field; only when BOTH agree is it treated as
    # decoration (skipped by the confidence gate, marked internal instead of filed standalone).
    ambiguous_image: bool = False
    # The attachment content PDF without its full source-note cover. Used to build the
    # combined package with a slim divider instead of repeating the cover metadata.
    source_pdf_path: Path | None = None


@dataclass
class PreparedEmail:
    email: EmailMessageData
    email_body_pdf: Path
    combined_pdf: Path
    artifacts: list[PreparedArtifact]
    text_preview: str
    issues: list[str]


@dataclass
class ClassificationResult:
    entity: str | None
    level2: str | None
    level3: str | None
    file_summary: str
    confidence: float
    unknown_entity: bool
    needs_review: bool
    reason: str
    action: str = "needs_review"
    document_date: str | None = None
    needs_review_reason: str | None = None
    urgent: bool = False
    decision_audit: dict = field(default_factory=dict)


@dataclass
class FilingTarget:
    entity: str | None
    level2: str | None
    level3: str | None
    folder_path: str
    drive_folder_id: str | None
    review: bool = False
