import logging
import re
import threading
import zipfile
from email.utils import parseaddr
from html import escape
from pathlib import Path

import img2pdf
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text
from pypdf import PdfReader, PdfWriter

from app.core.config import get_settings
from app.services.drive_service import DriveService
from app.services.types import EmailAttachment, EmailMessageData, PreparedArtifact, PreparedEmail
from app.utils.files import ensure_dir, safe_filename
from app.utils.hashing import sha256_file
from app.utils.time import date_prefix

logger = logging.getLogger(__name__)

# Bound concurrent WeasyPrint renders per worker process. Email processing runs in a
# thread pool (asyncio.to_thread), so a burst of emails would otherwise render their
# HTML bodies all at once and spike memory. This threading.BoundedSemaphore caps how
# many render simultaneously; the rest block here and proceed as slots free up — every
# email still gets processed, just not all at the same instant. Read once at import;
# change WEASYPRINT_MAX_CONCURRENT_RENDERS and restart to retune.
_WEASYPRINT_RENDER_SEMAPHORE = threading.BoundedSemaphore(
    max(1, get_settings().weasyprint_max_concurrent_renders)
)


class PdfService:
    # The client requires the full email (with its quoted chain) preserved in the archive, so
    # we render every message in the thread. This is only a pathological safety valve against
    # an absurdly long thread; in practice no real email reaches it, so nothing is omitted.
    MAX_THREAD_MESSAGES = 200

    def __init__(self) -> None:
        self.settings = get_settings()
        self.drive = DriveService()

    def prepare_email(self, email: EmailMessageData, work_dir: Path) -> PreparedEmail:
        ensure_dir(work_dir)
        email_body_pdf = work_dir / "email_body.pdf"
        issues: list[str] = []
        try:
            self._email_body_to_pdf(email, email_body_pdf)
        except Exception as exc:
            issues.append(f"email body: conversion failed: {exc}")
            self._reportlab_text_pdf(f"Email body conversion failed.\n\nReason: {exc}", email_body_pdf)

        artifacts: list[PreparedArtifact] = [
            self._artifact_for_pdf("email_body", "email_body.pdf", email_body_pdf, None, self._preview(email.body_text))
        ]
        for index, attachment in enumerate(email.attachments, start=1):
            if attachment.part_classification_issue:
                issues.append(f"{attachment.filename}: {attachment.part_classification_issue}")
            prepared_items, item_issues = self._prepare_attachment(email, attachment, index, len(email.attachments), work_dir)
            artifacts.extend(prepared_items)
            issues.extend(item_issues)

        combined_pdf = work_dir / "combined_email_package.pdf"
        self._build_combined_package(email_body_pdf, artifacts, work_dir, combined_pdf)
        artifacts.append(self._artifact_for_pdf("combined_package", "combined_email_package.pdf", combined_pdf, None, ""))
        text_preview = self._preview("\n\n".join([email.body_text, *[item.text_preview for item in artifacts]]), limit=8000)
        return PreparedEmail(email=email, email_body_pdf=email_body_pdf, combined_pdf=combined_pdf, artifacts=artifacts, text_preview=text_preview, issues=issues)

    def prepare_single_document(self, email: EmailMessageData, work_dir: Path) -> PreparedEmail:
        # Drive uploads: a single document, NOT an email. Build ONLY the one attachment artifact --
        # no email_body, no combined_package, no email-framed cover page. The synthetic `email`
        # still carries the single file in `attachments[0]` so we reuse `_prepare_attachment`
        # verbatim (with cover=False). Returns a PreparedEmail shaped for the rest of the pipeline,
        # which already filters to kind=="attachment" downstream for uploads.
        ensure_dir(work_dir)
        issues: list[str] = []
        attachment = email.attachments[0]
        if attachment.part_classification_issue:
            issues.append(f"{attachment.filename}: {attachment.part_classification_issue}")
        artifacts, item_issues = self._prepare_attachment(email, attachment, 1, 1, work_dir, cover=False)
        issues.extend(item_issues)
        doc = next((a for a in artifacts if a.kind == "attachment"), artifacts[0] if artifacts else None)
        doc_pdf = (doc.generated_pdf_path or doc.local_path) if doc else (work_dir / "missing.pdf")
        text_preview = self._preview("\n\n".join(a.text_preview for a in artifacts), limit=8000)
        # email_body_pdf / combined_pdf are unused for uploads (nothing files them) -- point them at
        # the document PDF to satisfy the PreparedEmail shape without generating extra files.
        return PreparedEmail(email=email, email_body_pdf=doc_pdf, combined_pdf=doc_pdf, artifacts=artifacts, text_preview=text_preview, issues=issues)

    def _prepare_attachment(
        self,
        email: EmailMessageData,
        attachment: EmailAttachment,
        index: int,
        total: int,
        work_dir: Path,
        cover: bool = True,
    ) -> tuple[list[PreparedArtifact], list[str]]:
        # cover=False (Drive uploads): skip the email-framed source-note cover page -- an uploaded
        # file has no email context and the original (not this generated PDF) is what gets filed,
        # so the generated PDF is only the classification copy.
        issues: list[str] = []
        if attachment.size_bytes > self.settings.max_file_size_mb * 1024 * 1024:
            return [
                PreparedArtifact(
                    kind="unsupported",
                    original_filename=attachment.filename,
                    local_path=attachment.local_path,
                    generated_pdf_path=None,
                    mime_type=attachment.mime_type,
                    text_preview="",
                    file_hash=sha256_file(attachment.local_path),
                    size_bytes=attachment.size_bytes,
                    issue=f"File exceeds {self.settings.max_file_size_mb}MB Phase 1 limit.",
                )
            ], [f"{attachment.filename}: oversized"]

        suffix = attachment.local_path.suffix.lower()
        if suffix == ".zip":
            return self._prepare_zip(email, attachment, index, total, work_dir)

        generated_dir = ensure_dir(work_dir / "generated")
        base = safe_filename(Path(attachment.filename).stem)
        output_pdf = generated_dir / f"{base}.prepared.pdf"
        text_preview = ""
        requires_claude_pdf = False
        page_count: int | None = None

        try:
            if suffix == ".pdf":
                # Detect a password-protected / encrypted PDF up front: it can't be read for text
                # OR sent to Claude vision (the API rejects it), and that's a PERMANENT condition,
                # so mark it unsupported here -> it goes to Needs Review (human unlocks/replaces it)
                # instead of wasting a Claude call and churning through retries.
                if self._is_encrypted_pdf(attachment.local_path):
                    return [
                        PreparedArtifact(
                            kind="unsupported",
                            original_filename=attachment.filename,
                            local_path=attachment.local_path,
                            generated_pdf_path=None,
                            mime_type=attachment.mime_type,
                            text_preview="",
                            file_hash=sha256_file(attachment.local_path),
                            size_bytes=attachment.size_bytes,
                            issue="PDF is password protected — can't be read. Provide an unlocked copy.",
                        )
                    ], [f"{attachment.filename}: password protected"]
                text_preview = self._extract_pdf_text(attachment.local_path)
                # One page-count parse, reused for the scanned check and passed to the classifier
                # so it doesn't re-parse to decide whether to truncate before sending to Claude.
                page_count = self._pdf_page_count(attachment.local_path)
                requires_claude_pdf = self._looks_scanned_pdf(attachment.local_path, text_preview, page_count)
                source_pdf = attachment.local_path
            elif suffix in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}:
                source_pdf = generated_dir / f"{base}.image.pdf"
                source_pdf.write_bytes(img2pdf.convert(str(attachment.local_path)))
                # An image attachment carries no extractable text (a screenshot/photo of a
                # document). Send it to Claude as a vision document so the receipt/letter content
                # is actually read -- otherwise the classifier sees only the filename and dumps it
                # to review. Decorative inline logos never reach here (they are inline_assets
                # embedded in the body, not kind="attachment").
                requires_claude_pdf = True
            elif suffix in {".txt", ".md", ".csv"}:
                text_preview = attachment.local_path.read_text(encoding="utf-8", errors="replace")
                source_pdf = generated_dir / f"{base}.text.pdf"
                self._reportlab_text_pdf(text_preview, source_pdf, title=attachment.filename)
            elif suffix in {".html", ".htm"}:
                html = attachment.local_path.read_text(encoding="utf-8", errors="replace")
                text_preview = BeautifulSoup(html, "html.parser").get_text("\n")
                source_pdf = generated_dir / f"{base}.html.pdf"
                self._reportlab_text_pdf(self._clean_thread_text(text_preview), source_pdf, title=attachment.filename)
            elif suffix in {".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"}:
                source_pdf = generated_dir / f"{base}.office.pdf"
                self.drive.convert_office_to_pdf(attachment.local_path, self._google_mime_for_office(suffix), source_pdf)
                text_preview = self._extract_pdf_text(source_pdf)
            else:
                return [
                    PreparedArtifact(
                        kind="unsupported",
                        original_filename=attachment.filename,
                        local_path=attachment.local_path,
                        generated_pdf_path=None,
                        mime_type=attachment.mime_type,
                        text_preview="",
                        file_hash=sha256_file(attachment.local_path),
                        size_bytes=attachment.size_bytes,
                        issue="Unsupported file type.",
                    )
                ], [f"{attachment.filename}: unsupported"]
            if cover:
                self._prepend_cover(email, attachment, index, total, source_pdf, output_pdf)
            else:
                # No cover page: the source PDF itself is the classification copy.
                output_pdf = source_pdf
        except Exception as exc:
            return [
                PreparedArtifact(
                    kind="unsupported",
                    original_filename=attachment.filename,
                    local_path=attachment.local_path,
                    generated_pdf_path=None,
                    mime_type=attachment.mime_type,
                    text_preview="",
                    file_hash=sha256_file(attachment.local_path),
                    size_bytes=attachment.size_bytes,
                    issue=str(exc),
                )
            ], [f"{attachment.filename}: conversion failed: {exc}"]

        return [
            PreparedArtifact(
                kind="attachment",
                original_filename=attachment.filename,
                local_path=attachment.local_path,
                generated_pdf_path=output_pdf,
                source_pdf_path=source_pdf,
                mime_type=attachment.mime_type,
                text_preview=self._preview(text_preview),
                file_hash=sha256_file(output_pdf),
                size_bytes=output_pdf.stat().st_size,
                requires_claude_pdf=requires_claude_pdf,
                page_count=page_count,
                ambiguous_image=attachment.part_classification_issue == "ambiguous_image_part",
            )
        ], issues

    def _prepare_zip(
        self,
        email: EmailMessageData,
        attachment: EmailAttachment,
        index: int,
        total: int,
        work_dir: Path,
    ) -> tuple[list[PreparedArtifact], list[str]]:
        extracted_root = ensure_dir(work_dir / "zip_extracted" / safe_filename(Path(attachment.filename).stem))
        total_size = 0
        max_total = self.settings.zip_max_extracted_mb * 1024 * 1024
        try:
            with zipfile.ZipFile(attachment.local_path) as archive:
                for info in archive.infolist():
                    depth = len(Path(info.filename).parts)
                    if depth > self.settings.zip_max_depth:
                        raise ValueError(f"ZIP depth exceeds {self.settings.zip_max_depth}")
                    total_size += info.file_size
                    if total_size > max_total:
                        raise ValueError(f"ZIP extracted size exceeds {self.settings.zip_max_extracted_mb}MB")
                archive.extractall(extracted_root)
        except Exception as exc:
            return [
                PreparedArtifact(
                    kind="unsupported",
                    original_filename=attachment.filename,
                    local_path=attachment.local_path,
                    generated_pdf_path=None,
                    mime_type=attachment.mime_type,
                    text_preview="",
                    file_hash=sha256_file(attachment.local_path),
                    size_bytes=attachment.size_bytes,
                    issue=f"ZIP risk or extraction failure: {exc}",
                )
            ], [f"{attachment.filename}: zip risk"]

        prepared: list[PreparedArtifact] = []
        issues: list[str] = []
        for inner in extracted_root.rglob("*"):
            if inner.is_dir():
                continue
            nested = EmailAttachment(filename=inner.name, mime_type=None, local_path=inner, size_bytes=inner.stat().st_size)
            nested_prepared, nested_issues = self._prepare_attachment(email, nested, index, total, work_dir)
            prepared.extend(nested_prepared)
            issues.extend(nested_issues)
        return prepared, issues

    def _email_body_to_pdf(self, email: EmailMessageData, output_pdf: Path) -> None:
        # Preferred path: render the real HTML body so inline cid: images (signatures,
        # logos, header/footer address blocks) are preserved. Falls back to the pure
        # ReportLab text layout for plain-text emails or hosts missing WeasyPrint's
        # native libs (GTK/Pango/cairo), so an email always produces a PDF.
        if email.body_html and email.body_html.strip():
            try:
                self._html_email_body_to_pdf(email, output_pdf)
                return
            except Exception as exc:
                logger.warning(
                    "WeasyPrint email PDF render failed, falling back to plain text: %s", exc, exc_info=True
                )
        self._structured_email_body_to_pdf(email, output_pdf)

    def _html_email_body_to_pdf(self, email: EmailMessageData, output_pdf: Path) -> None:
        # Embeds inline cid: assets as data URIs, blocks all remote resources, and renders
        # the email's own HTML with WeasyPrint. Imported lazily so a missing native lib
        # raises here and lets _email_body_to_pdf fall back to the text renderer.
        self._ensure_weasyprint_libs()
        from weasyprint import HTML

        ensure_dir(output_pdf.parent)
        cid_map = self._inline_cid_data_uris(email)
        body_html = self._rewrite_html_for_pdf(email.body_html, cid_map)
        document = self._compose_email_html(email, body_html)
        # Cap peak memory: only N renders run concurrently per worker (see module top).
        # A burst of emails queues here instead of all rendering at once.
        with _WEASYPRINT_RENDER_SEMAPHORE:
            HTML(string=document, url_fetcher=self._blocking_url_fetcher).write_pdf(str(output_pdf))

    @staticmethod
    def _ensure_weasyprint_libs() -> None:
        # WeasyPrint needs GTK/Pango/cairo/fontconfig native DLLs, which are not on the
        # default Windows search path. Point the loader at them via the
        # WEASYPRINT_DLL_DIRECTORIES env var (os.pathsep-separated bin dirs); for each,
        # derive FONTCONFIG_PATH from the sibling etc/fonts so generic font families
        # resolve to real Windows fonts. No-op on non-Windows / when the var is unset.
        import os
        import sys

        if not sys.platform.startswith("win"):
            return
        for raw in os.environ.get("WEASYPRINT_DLL_DIRECTORIES", "").split(os.pathsep):
            directory = raw.strip()
            if not directory or not os.path.isdir(directory):
                continue
            try:
                os.add_dll_directory(directory)
            except OSError:
                continue
            if not os.environ.get("FONTCONFIG_PATH"):
                fonts = os.path.normpath(os.path.join(directory, "..", "etc", "fonts"))
                if os.path.isfile(os.path.join(fonts, "fonts.conf")):
                    os.environ["FONTCONFIG_PATH"] = fonts

    def _cid_keys(self, content_id: str | None) -> set[str]:
        # Mirror gmail_service cid normalization: strip <>, unquote, lowercase, and also
        # index by the local-part before "@" so "image003@01DB" matches a bare "image003".
        from urllib.parse import unquote

        cleaned = unquote((content_id or "").strip()).strip().strip("<>").strip().lower()
        if not cleaned:
            return set()
        keys = {cleaned}
        if "@" in cleaned:
            keys.add(cleaned.split("@", 1)[0])
        return keys

    def _inline_cid_data_uris(self, email: EmailMessageData) -> dict[str, str]:
        import base64

        mapping: dict[str, str] = {}
        # Inline assets plus any real attachments that carry a Content-ID (e.g. a
        # footer/signature image whose cid wasn't detected in the HTML and so was
        # classified as a real attachment, but is still referenced via cid: in the body).
        sources = list(email.inline_assets) + [a for a in email.attachments if a.content_id]
        for asset in sources:
            if not asset.content_id or not asset.local_path or not asset.local_path.exists():
                continue
            try:
                raw = asset.local_path.read_bytes()
            except OSError:
                continue
            mime = asset.mime_type or "application/octet-stream"
            data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
            for key in self._cid_keys(asset.content_id):
                mapping.setdefault(key, data_uri)
        return mapping

    def _rewrite_html_for_pdf(self, body_html: str | None, cid_map: dict[str, str]) -> str:
        # Replace cid: images with their embedded data URI; drop scripts and remote
        # stylesheets; fetch remote images (signature logos, footer address blocks) and
        # embed them as data URIs so nothing reaches the network at WeasyPrint render time.
        soup = BeautifulSoup(body_html or "", "html.parser")
        for tag in soup(["script"]):
            tag.decompose()
        # Gmail conversation-thread HTML includes narrow right-side cells that contain
        # relative timestamps ("Tue, 10:47") and age labels ("(13 days ago)"). These
        # overflow/clip in a PDF page and add no filing value — remove them.
        _gmail_time_re = re.compile(r"""
            ^\s*(
                (Mon|Tue|Wed|Thu|Fri|Sat|Sun)[,.]?\s+\d{1,2}:\d{2}  # "Tue, 10:47"
                | \(\d+\s+day[s]?\s+ago\)                            # "(13 days ago)"
                | \d{1,2}:\d{2}\s*(AM|PM)?                           # bare "10:47"
            )\s*$
        """, re.VERBOSE | re.IGNORECASE)
        for td in soup.find_all(["td", "span", "div"]):
            text = td.get_text(strip=True)
            if _gmail_time_re.match(text) and not td.find(["img", "a"]):
                td.decompose()
        for link in soup.find_all("link"):
            if self._is_remote_url(link.get("href")):
                link.decompose()
        for img in soup.find_all("img"):
            self._apply_img_size_attrs(img)
            src = (img.get("src") or "").strip()
            low = src.lower()
            if low.startswith("cid:"):
                candidates = self._cid_keys(src[4:])
                data_uri = next((cid_map[key] for key in candidates if key in cid_map), None)
                if data_uri:
                    img["src"] = data_uri
                else:
                    img.decompose()
            elif low.startswith("data:"):
                continue
            elif not src:
                img.decompose()
            elif self._is_remote_url(src):
                data_uri = self._fetch_remote_image_data_uri(src)
                if data_uri:
                    img["src"] = data_uri
                else:
                    img.decompose()
        return str(soup)

    @staticmethod
    def _apply_img_size_attrs(img) -> None:
        # Email HTML commonly sizes images via the legacy width/height attributes
        # (e.g. a signature logo's underlying file is a higher-resolution PNG shown
        # at a smaller display size). Browsers and Gmail honor these as presentational
        # hints, but WeasyPrint does not, so the image would render at its full
        # intrinsic size. Translate them into inline CSS so the image scales the same.
        style = img.get("style") or ""
        has_width = re.search(r"(?<![\w-])width\s*:", style, re.I) is not None
        has_height = re.search(r"(?<![\w-])height\s*:", style, re.I) is not None
        size_pattern = re.compile(r"^\d+%?$")
        declarations = []
        width = (img.get("width") or "").strip()
        height = (img.get("height") or "").strip()
        if width and not has_width and size_pattern.match(width):
            unit = "" if width.endswith("%") else "px"
            declarations.append(f"width:{width}{unit}")
        if height and not has_height and size_pattern.match(height):
            unit = "" if height.endswith("%") else "px"
            declarations.append(f"height:{height}{unit}")
        if declarations:
            img["style"] = (style.rstrip(";") + ";" if style.strip() else "") + ";".join(declarations)

    @staticmethod
    def _is_remote_url(url: str | None) -> bool:
        low = (url or "").strip().lower()
        return low.startswith("http://") or low.startswith("https://") or low.startswith("//")

    @staticmethod
    def _fetch_remote_image_data_uri(url: str, timeout: float = 5.0, max_bytes: int = 5 * 1024 * 1024) -> str | None:
        # Best-effort fetch of an externally-hosted signature/footer image so it renders in
        # the PDF like it does in Gmail. Any failure (timeout, non-image, oversize, error
        # status) returns None so the caller drops the image instead of breaking the render.
        import base64
        import urllib.request

        target = url
        if target.startswith("//"):
            target = f"https:{target}"
        try:
            with urllib.request.urlopen(target, timeout=timeout) as resp:
                content_type = (resp.headers.get_content_type() or "").lower()
                if not content_type.startswith("image/"):
                    return None
                raw = resp.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    return None
        except Exception:
            return None
        return f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"

    def _compose_email_html(self, email: EmailMessageData, body_html: str) -> str:
        # Wrap the rewritten body in a header block mirroring the Gmail-print text layout
        # (bold subject, sender + right-aligned date, To/Cc) plus the attachment manifest.
        sender_name, sender_email = parseaddr(email.sender or "")
        if sender_name and sender_email:
            sender_disp = f"<strong>{escape(sender_name)}</strong> <span class='addr'>&lt;{escape(sender_email)}&gt;</span>"
        else:
            sender_disp = f"<strong>{escape(sender_name or sender_email or '(unknown sender)')}</strong>"

        meta = [f"<div class='meta'>To: {escape(email.recipient or '')}</div>"]
        if email.cc:
            meta.append(f"<div class='meta'>Cc: {escape(email.cc)}</div>")

        attachments_html = ""
        if email.attachments:
            items = "".join(
                f"<li>{escape(att.filename)} <span class='addr'>({escape(self._format_bytes(att.size_bytes))})</span></li>"
                for att in email.attachments
            )
            attachments_html = (
                f"<hr class='rule'/><div class='section'>{escape(self._attachment_heading(email.attachments))}</div>"
                f"<ul class='attachments'>{items}</ul>"
            )

        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'/>"
            f"<style>{self._email_pdf_css()}</style></head><body>"
            f"<div class='subject'>{escape(email.subject or '(no subject)')}</div>"
            "<hr class='rule'/>"
            f"<table class='header'><tr><td class='sender'>{sender_disp}</td>"
            f"<td class='date'>{escape(self._display_date(email.received_at))}</td></tr></table>"
            f"{''.join(meta)}"
            "<hr class='rule'/>"
            f"<div class='email-body'>{body_html}</div>"
            f"{attachments_html}"
            "</body></html>"
        )

    def _email_pdf_css(self) -> str:
        return (
            "@page { size: Letter; margin: 54pt; }"
            "body { font-family: Helvetica, Arial, sans-serif; font-size: 9.5pt; line-height: 1.4; color: #202124; }"
            ".subject { font-weight: bold; font-size: 15pt; margin-bottom: 6pt; }"
            ".rule { border: none; border-top: 0.7pt solid #dadce0; margin: 10pt 0; }"
            "table.header { width: 100%; border-collapse: collapse; }"
            ".sender { font-size: 9.5pt; text-align: left; }"
            ".date { font-size: 8.5pt; color: #5f6368; text-align: right; vertical-align: top; white-space: nowrap; }"
            ".meta { font-size: 8.5pt; color: #5f6368; }"
            ".addr { color: #5f6368; }"
            ".section { font-weight: bold; font-size: 9.5pt; margin-top: 8pt; }"
            "ul.attachments { margin: 4pt 0 0 0; padding-left: 16pt; font-size: 9.5pt; }"
            # Cap the body to roughly Gmail's reading-pane content width (~640px at 96dpi)
            # rather than the full page width, so images sized as 100%/oversized scale
            # down to about the same size shown in Gmail. Images with explicit pixel
            # width/height attributes are unaffected and already render 1:1 (WeasyPrint,
            # like browsers, treats CSS/HTML px at 96dpi).
            ".email-body { font-size: 9.5pt; max-width: 480pt; }"
            ".email-body img { max-width: 100%; height: auto; }"
            ".email-body table { max-width: 100%; }"
        )

    @staticmethod
    def _blocking_url_fetcher(url: str):
        # Hard guarantee against network fetches: only embedded data: URIs are allowed.
        from weasyprint import default_url_fetcher

        if url.startswith("data:"):
            return default_url_fetcher(url)
        raise ValueError(f"Remote resource blocked: {url[:80]}")

    def _structured_email_body_to_pdf(self, email: EmailMessageData, output_pdf: Path) -> None:
        # Gmail-print-style layout: bold subject title, sender line with the date
        # right-aligned, To/Cc metadata, the cleaned email thread, and the attachment
        # manifest with sizes. Pure ReportLab so it renders identically on every host
        # with no HTML-engine fragility or plain-text fallback.
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        ensure_dir(output_pdf.parent)
        styles = getSampleStyleSheet()
        gray = colors.HexColor("#5f6368")
        subject_style = ParagraphStyle("GmailSubject", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=15, leading=19)
        sender_style = ParagraphStyle("GmailSender", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13)
        meta_style = ParagraphStyle("GmailMeta", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=gray)
        date_style = ParagraphStyle("GmailDate", parent=meta_style, alignment=2)
        body_style = ParagraphStyle("GmailBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=13.5)
        section_style = ParagraphStyle("GmailSection", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=9.5, leading=12)

        sender_name, sender_email = parseaddr(email.sender or "")
        sender_html = f"<b>{self._paragraph_text(sender_name or sender_email or '(unknown sender)')}</b>"
        if sender_name and sender_email:
            sender_html += f" <font color='#5f6368'>&lt;{self._paragraph_text(sender_email)}&gt;</font>"
        header = Table(
            [[Paragraph(sender_html, sender_style), Paragraph(self._paragraph_text(self._display_date(email.received_at)), date_style)]],
            colWidths=[344, 160],
        )
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

        story: list = [
            Paragraph(self._paragraph_text(email.subject or "(no subject)"), subject_style),
            Spacer(1, 6),
            self._hr(),
            Spacer(1, 10),
            header,
            Spacer(1, 2),
            Paragraph(f"To: {self._paragraph_text(email.recipient or '')}", meta_style),
        ]
        if email.cc:
            story.append(Paragraph(f"Cc: {self._paragraph_text(email.cc)}", meta_style))
        story.extend([Spacer(1, 10), self._hr(), Spacer(1, 12)])

        thread = self._clean_thread_text(self._normalized_email_body_text(email))
        segments = self._segment_thread(thread)
        # The top (newest) message body renders flush; each quoted message below it gets a
        # Gmail-print-style header block, a divider, and a depth indent.
        omitted = 0
        if len(segments) > self.MAX_THREAD_MESSAGES:
            omitted = len(segments) - self.MAX_THREAD_MESSAGES
            segments = segments[: self.MAX_THREAD_MESSAGES]

        if not segments:
            story.append(Paragraph("(empty email body)", body_style))
        for position, segment in enumerate(segments):
            depth = min(position, 4)
            if segment["header"] is not None:
                story.extend(self._quote_header_flowables(segment["header"], depth, gray))
            indented_body = self._indented(body_style, depth)
            body_text = "\n".join(segment["body"]).strip()
            for paragraph in self._paragraphs(body_text):
                story.append(Paragraph(self._paragraph_text(paragraph) or "&nbsp;", indented_body))
                story.append(Spacer(1, 4))
        if omitted:
            story.extend([
                Spacer(1, 6),
                Paragraph(f"— {omitted} earlier message(s) in this thread omitted —", meta_style),
            ])

        if email.attachments:
            story.extend([
                Spacer(1, 8),
                self._hr(),
                Spacer(1, 8),
                Paragraph(self._paragraph_text(self._attachment_heading(email.attachments)), section_style),
                Spacer(1, 4),
            ])
            for index, attachment in enumerate(email.attachments, start=1):
                line = (
                    f"{index}. {self._paragraph_text(attachment.filename)} "
                    f"<font color='#5f6368'>({self._paragraph_text(self._format_bytes(attachment.size_bytes))})</font>"
                )
                story.append(Paragraph(line, body_style))
                story.append(Spacer(1, 2))

        doc = SimpleDocTemplate(
            str(output_pdf),
            pagesize=letter,
            rightMargin=54,
            leftMargin=54,
            topMargin=54,
            bottomMargin=54,
        )
        doc.build(story)

    def _hr(self):
        from reportlab.lib import colors
        from reportlab.platypus import HRFlowable

        return HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#dadce0"))

    def _segment_thread(self, text: str) -> list[dict]:
        # Split a cleaned email thread into ordered messages. The first segment is the top
        # (newest) message body; each later segment is a quoted reply/forward preceded by an
        # Outlook-style "From:/Sent:/To:/Cc:/Subject:" header block or a Gmail "On ... wrote:"
        # line. Each segment is {"header": dict | None, "body": list[str]}.
        lines = (text or "").split("\n")
        segments: list[dict] = [{"header": None, "body": []}]
        index = 0
        total = len(lines)
        header_field = re.compile(r"(?i)^(From|Sent|Date|To|Cc|Bcc|Subject)\s*:\s*(.*)$")
        on_wrote = re.compile(r"(?i)^On\b.+\bwrote:\s*$")
        while index < total:
            stripped = lines[index].strip()
            from_match = re.match(r"(?i)^From\s*:\s*(.+)$", stripped)
            is_outlook = bool(from_match) and any(
                re.match(r"(?i)^(Sent|Date)\s*:\s*", lines[look].strip())
                for look in range(index + 1, min(index + 7, total))
            )
            if is_outlook:
                header: dict[str, str] = {}
                cursor = index
                while cursor < total:
                    field = header_field.match(lines[cursor].strip())
                    if field:
                        header[field.group(1).lower()] = field.group(2).strip()
                        cursor += 1
                    elif lines[cursor].strip() == "" and cursor == index:
                        cursor += 1
                    else:
                        break
                segments.append({"header": header, "body": []})
                index = cursor
                continue
            if on_wrote.match(stripped):
                inner = re.match(r"(?i)^On\s+(.*?)\s+wrote:\s*$", stripped)
                segments.append({"header": {"on": inner.group(1).strip() if inner else stripped}, "body": []})
                index += 1
                continue
            segments[-1]["body"].append(lines[index])
            index += 1
        return [s for s in segments if s["header"] is not None or "\n".join(s["body"]).strip()]

    def _quote_header_flowables(self, header: dict, depth: int, gray) -> list:
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.platypus import Paragraph, Spacer

        base = getSampleStyleSheet()["BodyText"]
        indent = min(depth, 4) * 14
        name_style = ParagraphStyle(f"QuoteName{depth}", parent=base, fontName="Helvetica-Bold", fontSize=9, leading=12, leftIndent=indent)
        field_style = ParagraphStyle(f"QuoteField{depth}", parent=base, fontName="Helvetica", fontSize=8, leading=11, textColor=gray, leftIndent=indent)

        flowables: list = [Spacer(1, 8), self._hr(), Spacer(1, 6)]
        if "on" in header:
            flowables.append(Paragraph(f"On {self._paragraph_text(header['on'])} wrote:", field_style))
        else:
            if header.get("from"):
                flowables.append(Paragraph(self._paragraph_text(header["from"]), name_style))
            for label in ("sent", "date", "to", "cc", "subject"):
                value = header.get(label)
                if value:
                    flowables.append(Paragraph(f"{label.capitalize()}: {self._paragraph_text(value)}", field_style))
        flowables.append(Spacer(1, 5))
        return flowables

    def _indented(self, style, depth: int):
        from reportlab.lib.styles import ParagraphStyle

        return ParagraphStyle(f"{style.name}Indent{depth}", parent=style, leftIndent=min(depth, 4) * 14)

    def _display_date(self, received_at) -> str:
        if not received_at:
            return ""
        time_part = received_at.strftime("%I:%M %p").lstrip("0")
        return f"{received_at:%a, %b} {received_at.day}, {received_at.year}, {time_part}"

    def _attachment_heading(self, attachments: list[EmailAttachment]) -> str:
        count = len(attachments)
        return f"{count} attachment" if count == 1 else f"{count} attachments"

    def _format_bytes(self, size_bytes: int | None) -> str:
        if not size_bytes:
            return "size unknown"
        units = ["B", "KB", "MB", "GB"]
        value = float(size_bytes)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
            value /= 1024
        return f"{size_bytes} B"

    def _clean_thread_text(self, text: str) -> str:
        # Forwarded Gmail HTML fragments senders/emails across many lines and embeds icon
        # glyphs the core PDF font cannot draw. Reassemble the common patterns and drop
        # unrenderable glyphs so the thread reads cleanly.
        text = text or ""
        # Strip plain-text quote markers ("> ", ">> ") so quoted replies read cleanly.
        text = re.sub(r"(?m)^[ \t]*>+[ \t]?", "", text)
        # Strip plain-text emphasis asterisks ("*Bold*", "*They/Them*") Gmail renders as bold.
        text = re.sub(r"\*(?=\S)([^*\n]+?)(?<=\S)\*", r"\1", text)
        text = re.sub(r"<\s*\n\s*([^<>\n]+?)\s*\n\s*>", r"<\1>", text)
        text = re.sub(r"<\s*\n\s*([^<>\n]+)", r"<\1", text)
        text = re.sub(r"([^<>\n]+?)\s*\n\s*>", r"\1>", text)
        text = re.sub(r"(?im)^(From|To|Cc|Bcc|Sent|Date|Subject)\s*:\s*\n\s*", r"\1: ", text)
        email_only = re.compile(r"^<?[^<>\s@]+@[^<>\s@]+>?[.,;]?$")
        merged: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if merged and stripped and email_only.match(stripped):
                previous = merged[-1].strip()
                if previous and not email_only.match(previous):
                    merged[-1] = merged[-1].rstrip() + " " + stripped
                    continue
            merged.append(line)
        text = "\n".join(merged)
        # Drop glyphs the core Helvetica (WinAnsi) font cannot render (icons/emoji).
        text = text.encode("cp1252", "ignore").decode("cp1252")
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _normalized_email_body_text(self, email: EmailMessageData) -> str:
        text = (email.body_text or "").strip()
        if not text and email.body_html:
            text = BeautifulSoup(email.body_html, "html.parser").get_text("\n")
        return self._normalize_text(text)

    def _normalize_text(self, text: str) -> str:
        text = (text or "").replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n<\n([^>\n]+)\n>", r" <\1>", text)
        text = re.sub(r"\n<([^>\n]+)>\n", r" <\1>\n", text)
        text = re.sub(r"(?m)^([A-Za-z][A-Za-z .'-]{1,80})\n<([^>\n]+)>", r"\1 <\2>", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _google_mime_for_office(self, suffix: str) -> str:
        if suffix in {".doc", ".docx"}:
            return "application/vnd.google-apps.document"
        if suffix in {".xls", ".xlsx", ".csv"}:
            return "application/vnd.google-apps.spreadsheet"
        if suffix in {".ppt", ".pptx"}:
            return "application/vnd.google-apps.presentation"
        raise ValueError(f"Unsupported Office conversion type: {suffix}")

    def _write_text_pdf(self, title: str, sections: list[tuple[str, str]], output_pdf: Path) -> None:
        # Structured ReportLab document: centered bold title, bold field labels, and body
        # paragraphs. Pure-Python (no native deps) so it renders reliably on every host.
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        ensure_dir(output_pdf.parent)
        styles = getSampleStyleSheet()
        styles["BodyText"].fontName = "Helvetica"
        styles["BodyText"].fontSize = 9
        styles["BodyText"].leading = 12
        doc = SimpleDocTemplate(
            str(output_pdf),
            pagesize=letter,
            rightMargin=54,
            leftMargin=54,
            topMargin=54,
            bottomMargin=54,
        )
        story = [Paragraph(self._paragraph_text(title), styles["Title"]), Spacer(1, 18)]
        for heading, text in sections:
            if heading:
                story.append(Paragraph(f"<b>{self._paragraph_text(heading)}</b>", styles["Heading3"]))
            for paragraph in self._paragraphs(self._safe_text(text)):
                story.append(Paragraph(self._paragraph_text(paragraph) or "&nbsp;", styles["BodyText"]))
                story.append(Spacer(1, 4))
            story.append(Spacer(1, 10))
        doc.build(story)

    def _reportlab_text_pdf(self, text: str, output_pdf: Path, title: str = "Attachment Text") -> None:
        self._write_text_pdf(title, [("", text or "(No extractable text.)")], output_pdf)

    def _safe_text(self, value) -> str:
        return str(value or "").replace("\x00", "")

    def _paragraph_text(self, value) -> str:
        return escape(self._safe_text(value)).replace("\n", "<br/>")

    def _paragraphs(self, text: str) -> list[str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(line.strip() for line in text.splitlines())
        blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
        if not blocks and text.strip():
            blocks = [text.strip()]
        return blocks or [""]

    def _cover_page(self, email: EmailMessageData, attachment: EmailAttachment, index: int, total: int, output_pdf: Path) -> None:
        source_note = (
            f"Email received {date_prefix(email.received_at)} from {email.sender or ''} "
            f"to {email.recipient or ''}. Subject: {email.subject or ''}. "
            f"Attachment {index} of {total}: {attachment.filename}."
        )
        self._write_text_pdf(
            "Attachment Source Note",
            [
                ("Received", date_prefix(email.received_at)),
                ("From", email.sender or ""),
                ("To", email.recipient or ""),
                ("Subject", email.subject or ""),
                ("Original Attachment", attachment.filename),
                ("Source Note", source_note),
            ],
            output_pdf,
        )

    def _prepend_cover(self, email: EmailMessageData, attachment: EmailAttachment, index: int, total: int, source_pdf: Path, output_pdf: Path) -> None:
        cover = output_pdf.with_suffix(".cover.pdf")
        self._cover_page(email, attachment, index, total, cover)
        self._merge_pdfs([cover, source_pdf], output_pdf)

    def _divider_page(self, index: int, total: int, filename: str | None, output_pdf: Path) -> None:
        # Slim separator used inside the combined package. The full source-note metadata
        # already lives on the Email Source page, so this only identifies the attachment.
        self._write_text_pdf(f"Attachment {index} of {total}", [("File", filename or "")], output_pdf)

    def _build_combined_package(self, email_body_pdf: Path, artifacts: list[PreparedArtifact], work_dir: Path, output_pdf: Path) -> None:
        # Email Source page first, then each attachment's content preceded by a slim
        # divider (not its full cover). Standalone attachment files keep their cover.
        divider_dir = ensure_dir(work_dir / "generated")
        inputs: list[Path] = [email_body_pdf]
        attachment_artifacts = [item for item in artifacts if item.kind == "attachment"]
        total = len(attachment_artifacts)
        for position, artifact in enumerate(attachment_artifacts, start=1):
            content = artifact.source_pdf_path or artifact.generated_pdf_path or artifact.local_path
            if not content:
                continue
            divider = divider_dir / f"divider_{position}.pdf"
            self._divider_page(position, total, artifact.original_filename, divider)
            inputs.append(divider)
            inputs.append(content)
        self._merge_pdfs(inputs, output_pdf)

    def _merge_pdfs(self, pdfs: list[Path], output_pdf: Path) -> None:
        writer = PdfWriter()
        for pdf in pdfs:
            if not pdf.exists():
                continue
            reader = PdfReader(str(pdf))
            for page in reader.pages:
                writer.add_page(page)
        with output_pdf.open("wb") as handle:
            writer.write(handle)

    def _extract_pdf_text(self, path: Path) -> str:
        try:
            return extract_text(str(path)) or ""
        except Exception:
            return ""

    def _pdf_page_count(self, path: Path) -> int:
        try:
            return max(1, len(PdfReader(str(path)).pages))
        except Exception:
            return 1

    def _is_encrypted_pdf(self, path: Path) -> bool:
        # True when the PDF is password-protected/encrypted such that its pages can't be read
        # without a password. pypdf sets .is_encrypted; an empty-password decrypt sometimes works
        # (owner-only permissions) so we try that before declaring it truly locked.
        try:
            reader = PdfReader(str(path))
            if not reader.is_encrypted:
                return False
            try:
                # Returns 0 (FAILED) when the empty password does NOT unlock it.
                if reader.decrypt("") != 0:
                    return False
            except Exception:
                pass
            return True
        except Exception:
            # Unreadable header etc. -> not our concern here; let normal extraction handle it.
            return False

    def _looks_scanned_pdf(self, path: Path, text: str, page_count: int | None = None) -> bool:
        pages = page_count if page_count is not None else self._pdf_page_count(path)
        return len(text.strip()) < pages * 50

    def _preview(self, text: str, limit: int = 8000) -> str:
        # ~first few pages of extractable text. Bigger default so attachment previews carry
        # the Level-3 details (account/policy numbers, statement periods) Claude needs.
        return (text or "").strip()[:limit]

    def _artifact_for_pdf(
        self,
        kind: str,
        original_filename: str | None,
        path: Path,
        mime_type: str | None,
        text_preview: str,
    ) -> PreparedArtifact:
        return PreparedArtifact(
            kind=kind,
            original_filename=original_filename,
            local_path=path,
            generated_pdf_path=path,
            mime_type=mime_type or "application/pdf",
            text_preview=text_preview,
            file_hash=sha256_file(path),
            size_bytes=path.stat().st_size,
        )
