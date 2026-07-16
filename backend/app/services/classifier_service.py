import base64
import json
import re
from email.utils import getaddresses
from pathlib import Path

from anthropic import Anthropic
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Entity
from app.services.learning_service import LearningService
from app.services.rulebook_service import RulebookService
from app.services.signals import extract_addresses, extract_signals, normalize_org
from app.services.types import ClassificationResult, PreparedEmail
from app.services.usage_guard import ApiUsageGuard


class ClassifierService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.rulebook = RulebookService()
        self.learning = LearningService()
        self.usage_guard = ApiUsageGuard()

    def classify(self, db: Session, prepared: PreparedEmail, entities: list[Entity]) -> ClassificationResult:
        if self.settings.classifier_mode.lower() != "claude":
            return self._mock_classify(prepared, entities)
        if not self.settings.enable_real_claude:
            raise RuntimeError("ENABLE_REAL_CLAUDE=true is required before calling the paid Claude API.")
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when CLASSIFIER_MODE=claude.")
        self.usage_guard.assert_available(db, "claude", self.settings.claude_daily_call_limit)
        result = self._claude_classify(db, prepared, entities)
        self.usage_guard.increment(db, "claude", self.settings.claude_daily_call_limit)
        return result

    def _claude_classify(self, db: Session, prepared: PreparedEmail, entities: list[Entity]) -> ClassificationResult:
        # max_retries: the SDK retries 5xx/429 with backoff. Raised above the default of 2 so a
        # burst of transient Anthropic 500s (seen on backlog flush) is absorbed, not failed.
        client = Anthropic(api_key=self.settings.anthropic_api_key, max_retries=5)
        # One typed-signal extraction drives both the learned-mapping lookup and (after a
        # review) what gets learned, so classification and learning always agree on what is
        # discriminating about this email.
        forwarder_domains = {d.strip().lower() for d in self.settings.forwarder_domains or []}
        signals = extract_signals(
            sender=prepared.email.sender,
            recipient=prepared.email.recipient,
            cc=prepared.email.cc,
            subject=prepared.email.subject,
            body_text=prepared.email.body_text,
            forwarder_domains=forwarder_domains,
        )
        mappings = self.learning.top_relevant(
            db, prepared.email.sender, prepared.email.subject, signals=signals
        )
        contact_hints = self._contact_hints(prepared)
        prompt = self._prompt(prepared, entities, mappings, contact_hints, signals)[: self.settings.claude_max_prompt_chars]
        content: list[dict] = [{"type": "text", "text": prompt}]
        # Bound the combined image-document payload so several large scans can't overflow the
        # model's context window; once spent, later scanned PDFs ride on their text preview only.
        image_budget = self.settings.claude_pdf_total_max_mb * 1024 * 1024
        image_total = 0
        for artifact in prepared.artifacts:
            pdf_path = artifact.generated_pdf_path or artifact.local_path
            if not artifact.requires_claude_pdf or not pdf_path or not pdf_path.exists():
                continue
            if pdf_path.stat().st_size > self.settings.claude_pdf_max_mb * 1024 * 1024:
                continue  # too big to send as an image -> classified from its text preview
            send_path = self._pdf_for_claude(pdf_path, artifact.page_count)
            if send_path is None:
                continue  # unreadable -> rely on the text preview rather than risk an API error
            data = send_path.read_bytes()
            if image_total + len(data) > image_budget:
                continue  # per-email image budget spent -> rely on this attachment's preview
            image_total += len(data)
            content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )

        response = client.messages.create(
            model=self.settings.claude_model,
            max_tokens=self.settings.claude_max_output_tokens,
            temperature=0,
            messages=[{"role": "user", "content": content}],
        )
        text = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        result = self._parse_json_response(text)
        # Persist the typed signals so review_service can learn from ALL of them after a
        # correction/approval. The body (with the forwarded chain) is gone by review time —
        # ProcessedEmail has no body column — so the signals must be captured here.
        result.decision_audit["learn_signals"] = signals
        # Kept for backward compatibility with items filed before learn_signals existed.
        original_sender_email = self._get_original_sender_email(contact_hints)
        if original_sender_email:
            result.decision_audit["original_sender_email"] = original_sender_email
        return result

    def _pdf_for_claude(self, pdf_path: Path, known_page_count: int | None = None) -> Path | None:
        # The path to send to Claude as an image document. Claude rejects PDFs over
        # claude_pdf_max_pages, so an over-limit (scanned) PDF is truncated to its first N pages
        # written alongside the original -- enough to read the entity/header, without erroring the
        # whole classification. Returns None for an unreadable PDF so the caller falls back to the
        # text preview instead of risking a bad request.
        max_pages = self.settings.claude_pdf_max_pages
        # Fast path: prep already counted the original's pages. The sent PDF adds a cover page, so
        # a 2-page margin keeps us safe -- when comfortably under the cap, skip the re-parse.
        if known_page_count is not None and known_page_count <= max_pages - 2:
            return pdf_path
        try:
            reader = PdfReader(str(pdf_path))
            page_count = len(reader.pages)
        except Exception:
            return None
        if page_count <= max_pages:
            return pdf_path
        try:
            writer = PdfWriter()
            for page in reader.pages[:max_pages]:
                writer.add_page(page)
            capped = pdf_path.with_suffix(f".first{max_pages}.pdf")
            with open(capped, "wb") as handle:
                writer.write(handle)
            return capped
        except Exception:
            return None

    def _prompt(self, prepared: PreparedEmail, entities: list[Entity], mappings: list, contact_hints: list[dict] | None = None, signals: list[dict] | None = None) -> str:
        # Drive uploads are a single document, not an email. The rules below are written for email,
        # so for an upload we PREPEND an override that reframes the task as a document and tells the
        # model to ignore the email-specific expectations (no sender/subject by design). Everything
        # else (entity matching, building-number rule, Level-2/3 format) still applies unchanged.
        is_upload = (getattr(prepared.email, "raw_metadata", None) or {}).get("source") == "drive_upload"
        upload_preamble = (
            "IMPORTANT - SOURCE OVERRIDE: This is NOT an email. It is a SINGLE DOCUMENT that a user\n"
            "uploaded directly into a Drive folder for filing. There is intentionally no sender,\n"
            "recipient, subject, or email body -- do NOT mention any of those as missing, and do NOT\n"
            "treat the absence of email metadata as a reason for review. Classify purely from the\n"
            "document's own content and filename. Write file_summary and `reason` about the DOCUMENT\n"
            "(e.g. 'Bank statement for ...'), never 'the email'. Set email_sender to null. Do not\n"
            "reject as spam/newsletter -- an uploaded file is intended for filing. Apply all the\n"
            "filing rules below (entity match, building-number rule, Level-2/3 format) as written.\n\n"
            if is_upload else ""
        )
        if contact_hints is None:
            contact_hints = self._contact_hints(prepared)
        entity_phrase_hints = self._entity_phrase_hints(prepared)
        # Always show every entity NAME (full recall, cheap) ordered most-relevant first; show
        # the heavier aliases/properties only for the most relevant subset (bounded detail).
        ordered_entities = self._scored_entities(entities, prepared, mappings, entity_phrase_hints, signals)
        names_json = json.dumps([item.entity_name for item in ordered_entities], ensure_ascii=False)
        names_section = self._truncate(names_json, 6000)
        names_note = "(too many to list in full; most relevant shown first)\n" if len(names_json) > len(names_section) else ""
        detail_payload = [
            {"entity_name": item.entity_name, "aliases": item.aliases, "properties": item.properties}
            for item in ordered_entities[: self.ENTITY_DETAIL_TOP_N]
        ]
        mapping_payload = [
            {
                "pattern_type": item.pattern_type,
                "pattern_value": item.pattern_value,
                "entity": item.entity,
                "level2": item.level2,
                "level3": item.level3,
                "confidence_boost": item.confidence_boost,
                "confirmation_count": item.confirmation_count,
                "label": "high_confidence_example" if item.confirmation_count >= 5 else "confirmed_example",
                # Reviewer's rationale from a past Correct on a matching sender/keyword, so the
                # model learns *why* similar mail was filed here (capped to bound prompt size).
                "reviewer_note": self._truncate(item.note, 200) if getattr(item, "note", None) else None,
            }
            for item in mappings
        ]
        return f"""{upload_preamble}You are classifying a real estate accounting filing {"document" if is_upload else "email"} for RRES.

Return only JSON with keys:
action, entity, level2, level3, file_summary, artifact_summaries, email_sender, document_date, confidence, unknown_entity, additional_entities, needs_review_reason, reason.

Rules:
- Action must be one of: file, needs_review, reject.
- Use reject only for clear non-filing items such as spam, ads, newsletters, unrelated social media, or system notifications.
- Known entities are the current Drive Level 1 client/entity folders. Use them as the filing source of truth.
- If a Known entity or one of its aliases/properties matches, return the exact Known entity_name and set unknown_entity=false.
- A street/property address matches a Known entity ONLY when the building/house NUMBER is the
  same. Different building numbers on the same street are different properties and almost always
  different entities. Example: an email about "1416 Frankford" must NOT be filed to an entity for
  "1828 Frankford Owner LLC" -- the street matches but 1416 != 1828. If no Known entity shares the
  email's building number (and no other strong identifier matches), treat the entity as unknown:
  propose a new Level 1 if a safe client/contact is present, otherwise set unknown_entity=true and
  action=needs_review. Never snap to the closest street name.
- When only a street name (not the building number) matches a Known entity, keep confidence at or
  below 50 and state the building-number mismatch in needs_review_reason.
- additional_entities: a JSON array of any OTHER Known entity_name(s) this email or its attachments
  ALSO belong to (e.g. one email carrying separate reports for two different properties). Set entity
  to your best PRIMARY entity AND list the other distinct Known entities here. Use [] when everything
  belongs to a single entity. Do NOT list the primary entity itself here.
- The filing system CAN automatically split a multi-entity email: it files each attachment to the
  entity you assign it in artifact_summaries ("entity"), and copies the email itself to every
  involved entity. Set action="file" (NOT needs_review) purely based on whether you are confident
  in the classification -- do NOT downgrade to needs_review merely because more than one entity is
  involved. The backend decides whether to auto-split or route to review based on entity confidence.
  IMPORTANT: do NOT force an attachment to a Known entity just to enable auto-split. If an
  attachment's entity is unknown or not in the Known entity list, propose it in "F. Last - Entity Name"
  format using the same client/owner principal you identified for the email (e.g. if the primary
  entity is "J. Claffey - 1322 Frankford Owner LLC" and another attachment belongs to an unknown
  "2002 Frankford Owner LLC", propose "J. Claffey - 2002 Frankford Owner LLC"). Only omit the
  client prefix when there is no owner principal identifiable from the email context.
  Set entity_confidence LOW -- the system will route it to review.
- If no Known entity matches, but the email clearly shows both a non-RRES human client/contact and a property/entity name,
  propose the Level 1 folder in this exact format: "F. Last - Entity Name". Example: "Jane Doe" + "123 Street LLC" => "J. Doe - 123 Street LLC".
  Set unknown_entity=true and action=needs_review because the folder does not exist yet.
- When forming an unknown Level 1 proposal, use the human requester/client/contact from the business thread.
  Do not use RRES staff, file-cabinet/forwarder inboxes, vendors, banks, insurance carriers, counties, government agencies, or system senders as the person.
- The Level 1 principal is the property OWNER / sponsor / managing member that RRES keeps the books for -- never a counterparty.
  When RRES is the sender or forwarder delivering reports outward, the direct recipient is usually a counterparty (lender, loan servicer, investor, buyer), NOT the client. Do not pick someone just because the email is addressed to them.
  Read signatures and titles in the thread: prefer a person whose role is owner, principal, sponsor, managing partner, managing member, or manager OF THE ENTITY. Reject as the principal anyone whose role is lender, loan acquisitions, loan servicing, mortgage, bank, escrow, title, or counterparty investor relations.
  Example: RRES emails monthly financials to a "Loan Acquisitions Specialist" at a lender, with the deal's managing partner CC'd -- the principal is the managing partner (the owner side), not the lender recipient.
- The Contact hints carry a role_side for each person ("owner", "counterparty", or null). Never choose a contact whose role_side is "counterparty" as the principal. Prefer a contact whose role_side is "owner". Trust role_side over the email domain. If every candidate is "counterparty" or null, treat the owner principal as unidentified and set "principal ambiguous".
- If the email shows only a property/entity name and no safe human client/contact, do not invent a person.
  Return the raw entity/property name, set unknown_entity=true, action=needs_review, and mention that the principal/client name is missing.
- If two or more people could plausibly be the owner principal, still propose your best guess but set confidence at or below 50 and write "principal ambiguous" in needs_review_reason.
- Confidence must be a number from 0 to 100, not 0 to 1. Example: use 82, not 0.82.
- Unknown entities must set unknown_entity=true and action=needs_review.
- Ask Client - Closed is a normal client folder, never a system fallback.
- Needs Review is the system review workflow, not a Level 2 folder.
- "Client Uploads" and "RRES Uploads" are drop-off folders where people place files to be filed --
  they are NEVER a filing destination. Never classify a document INTO "Client Uploads" or
  "RRES Uploads"; pick the substantive category for the document's actual content.
- Choose Level 2 only from this rulebook list: {self.rulebook.allowed_level2()}.
- Level 3 must be null when the selected Level 2 folder has subfolder_rule="none".
- Level 3 folder rules by Level 2: {json.dumps(self.rulebook.level3_rules(), ensure_ascii=False)}.
- Level 3 folder names must use these EXACT formats. The last 4 digits go in PARENTHESES at the
  end (no "Checking"/"Savings" words):
  by_bank = "<Bank Name> (<last 4 digits of account>)" (example: "Chase Bank (1234)", "TD Bank (6789)"),
  by_credit_card = "<Card Name> (<last 4 digits>)" (example: "American Express (4567)"),
  by_lender = "<Lender Name>" (example: "Castellan Capital"),
  by_year = "<YYYY>" (example: "2026").
  Correct: "Chase Bank (1234)". WRONG: "Chase Bank 1234", "Chase Bank #1234", "Chase Checking (1234)".
- Use the institution's recognizable brand name in Level 3 ("Chase Bank", not "Chase"/"JPMC";
  "M&T Bank", not "MT"). For lenders, drop corporate-form suffixes like "Real Estate Partners",
  "LLC", "Group", "Partners" and use the lender's known brand -- e.g. "Castellan Capital", NOT
  "Castellan Real Estate Partners". If a learned mapping already names this institution for this
  entity, reuse that exact name. Never append account-type words such as Checking, Savings, or Business.
- Read the last 4 digits from the account number inside the document, never from the filename.
- Dual filing: a single PDF of the whole email (body + attachments) is ALWAYS archived to the
  entity's Communications folder automatically -- you do not classify it. Each real attachment
  is instead filed to ITS OWN Level 2 based on the attachment's own content. So never classify
  an attachment as Communications; pick the substantive category (Bank Statements, Insurance,
  Property Taxes, Client Reporting, Leases, etc.) for each attachment.
- The email-level level2/level3 must be the category of the MOST significant attachment (used as
  the fallback). Use Communications for level2 only when the email has no real attachments.
- File summary must be a concise filename-ready phrase following this format:
  "{{Document description}} for {{Property or Entity reference}}"
  Example: "State Farm Insurance Declarations for 1322 Frankford Owner LLC"
  Example: "Hartford Business Owners Past Due Bill 44SBABR5BL6 for 1322 Frankford Owner"
  Example: "Foremost Rent Roll for Foremost Professional Plaza"
  Rules:
  - Never include any date, month, or year (the date prefix already carries it).
  - Never write "Email Regarding", sentences, coverage limits, or billing amounts.
  - Always include a property or entity reference so the filename is self-identifying without
    opening the folder — even for single-entity emails where the folder already carries the name.
  - Include a policy/account number only when needed to tell apart multiple documents of the same type.
  - Keep it under 12 words total.
- When two or more attachments in the same email share the same document type (e.g. all are Owner
  Reports or all are Cash Basis Financial Reports), each summary MUST include the property name or
  LLC short name to tell them apart in Drive — even though they file to different entity folders.
  Example: four owner reports ->
    "Owner Report for 1322 Frankford Owner LLC",
    "Owner Report for 2002 Frankford Owner LLC",
    "Owner Report for 1603 Frankford Owner LLC",
    "Owner Report for Queen Village Owner LLC".
  Without distinct property references, the filing system cannot distinguish the files.
- artifact_summaries is an object keyed by artifact kind or original attachment filename. Each
  value must be an object: {{"summary": "<filename phrase>", "level2": "<Level 2 for THIS attachment>",
  "level3": "<Level 3 or null per the rules above>", "entity": "<exact Known entity_name THIS
  attachment belongs to>", "entity_confidence": <0-100>}}. Use the key "email_body" for the email
  body (summary only; its level2/level3/entity may be null). Use the original attachment filename
  as the key for each attachment, EXACTLY as given in "Prepared artifacts" below.
- Classify each attachment's "entity" INDEPENDENTLY using this signal priority order:
  1. The attachment's document content (first page body text, header, letterhead) -- highest authority.
  2. The attachment's filename -- strong supporting signal when content is ambiguous or generic.
  3. The outer email subject/thread -- lowest authority; use only when both content and filename give no signal.
  One email may carry documents for different clients -- give each attachment its own owning entity.
  Building-number rule (strict, applies per attachment): a street address or LLC name matches a
  Known entity ONLY when the building NUMBER is EXACTLY identical -- not approximately, not nearby.
  MUST NOT snap to a different building number on the same street. Examples of WRONG behaviour:
    "2002 Frankford Owner LLC" -> MUST NOT match "J. Claffey - 1700-04 Frankford" (wrong number)
    "1603 Frankford Owner LLC" -> MUST NOT match "J. Claffey - 1322 Frankford Owner LLC" (wrong number)
  If no Known entity has that exact building number, set entity to the LLC name from the filename
  (e.g. "2002 Frankford Owner LLC") and set entity_confidence LOW (below 50). Never guess a
  nearby entity just because it is on the same street.
  Cross-check rule: when the filename and document content AGREE on the entity, set entity_confidence
  high (85+). When they DISAGREE (filename says "ABC LLC" but the document body says "XYZ LLC"), set
  entity_confidence low (below 50) and note the discrepancy in needs_review_reason -- the email will
  go to human review. Never override a clear document-content signal with the outer email subject.
  Set "entity" to the exact Known entity_name when matched; set "entity_confidence" to how sure you
  are (0-100). If an attachment's entity is unclear or not a Known entity, propose the entity name
  from the filename/content and set a LOW entity_confidence -- the email will be sent to human review.
- Decorative images: some image attachments are marked "possibly_decorative": true in Prepared
  artifacts -- the mail parser suspects they are signature logos/banners rather than documents.
  For EACH such attachment, look at its content and add a "decorative" field to its
  artifact_summaries entry: true when it is decoration (company logo, signature graphic, banner,
  social-media icon, headshot), false when it is a real document (screenshot or photo of a
  statement, receipt, lease, invoice, letter). A decorative image is already preserved inside the
  archived email PDF -- never treat it as evidence of another client, never count it toward
  multi-entity decisions, and never let it lower your confidence.
- Attachment summaries must describe the attachment itself, not just the forwarded email thread.
- For insurance policy PDFs, include carrier, policy type, policy number, and entity when visible.
- email_sender: the display name of the ORIGINAL sender of the forwarded content (the innermost
  "From:" in the thread), used to name the archived email; null if not determinable.
- document_date should be ISO YYYY-MM-DD when visible in the document; otherwise null.

Email:
From: {prepared.email.sender}
To: {prepared.email.recipient}
Subject: {prepared.email.subject}
Received: {prepared.email.received_at}

Contact hints extracted from the email thread (not authoritative):
{json.dumps(contact_hints, ensure_ascii=False)}

Entity/property phrase hints extracted from the subject/body (not authoritative):
{json.dumps(entity_phrase_hints, ensure_ascii=False)}

Known entities -- the complete list of Drive Level 1 folder names. Match the email against ANY of these:
{names_note}{names_section}

Known entity details (aliases/properties for the most likely matches only; an entity missing here is still valid -- match it by name in the list above):
{self._truncate(json.dumps(detail_payload, ensure_ascii=False), 3500)}

Learned mappings:
{self._truncate(json.dumps(mapping_payload, ensure_ascii=False), 1000)}

Text preview:
{self._truncate(prepared.text_preview, 12000)}

Prepared artifacts:
{self._truncate(json.dumps(self._artifact_prompt_payload(prepared), ensure_ascii=False), self.settings.claude_artifact_payload_chars)}

Conversion issues:
{self._truncate(str(prepared.issues), 400)}
"""

    def _get_original_sender_email(self, contact_hints: list[dict]) -> str | None:
        # Return the email address of the owner-side contact from the forwarded chain so the
        # review service can record a sender/domain mapping after a correction or approval.
        # We only use contacts with a confirmed "owner" role_side to avoid recording a
        # counterparty (lender, servicer) or the connected relay inbox as the learned sender.
        for contact in contact_hints:
            if contact.get("principal_candidate") and contact.get("role_side") == "owner":
                return contact.get("email") or None
        return None

    def _contact_hints(self, prepared: PreparedEmail) -> list[dict]:
        raw_values = [
            prepared.email.sender or "",
            prepared.email.recipient or "",
            prepared.email.cc or "",
        ]
        # Scan the FULL body (not the truncated preview) for role/title signals, so the
        # owner-vs-counterparty hint survives the prompt-length cap.
        text = "\n".join([prepared.email.body_text or "", prepared.text_preview or ""])
        full_text = prepared.email.body_text or text
        for match in re.finditer(r"(?im)^(From|To|Cc):\s*(.+)$", text):
            raw_values.append(match.group(2))

        forwarder_domains = {domain.lower().strip() for domain in self.settings.forwarder_domains or []}
        output: list[dict] = []
        seen: set[str] = set()
        for name, email in getaddresses(raw_values):
            email = (email or "").strip().lower()
            display_name = self._clean_contact_name(name)
            if not email or not display_name:
                continue
            domain = email.split("@", 1)[1] if "@" in email else ""
            key = f"{display_name.lower()}|{email}"
            if key in seen:
                continue
            seen.add(key)
            blocked = self._blocked_principal_candidate(display_name, email, domain, forwarder_domains)
            role_hint, role_side = self._role_for_contact(full_text, display_name)
            output.append(
                {
                    "name": display_name,
                    "email": email,
                    "domain": domain,
                    "principal_candidate": not blocked,
                    "blocked_reason": blocked,
                    "role_hint": role_hint,
                    "role_side": role_side,
                    "formatted_name_if_used": self._format_person_name(display_name),
                }
            )
            if len(output) >= 20:
                break
        return output

    # Job-title signals used to tell the property owner/sponsor (the Level 1 principal) apart
    # from a counterparty (lender, servicer, title/escrow) who is never the principal.
    OWNER_ROLE_TERMS = [
        "managing partner", "managing member", "general partner", "principal",
        "owner", "sponsor", "founder", "president", "ceo", "managing director",
    ]
    COUNTERPARTY_ROLE_TERMS = [
        "loan acquisitions", "loan servicing", "loan officer", "loan administrator",
        "lender", "mortgage", "servicer", "escrow", "title officer", "underwriter",
        "investor relations", "acquisitions specialist", "acquisitions admin", "banker",
    ]

    # How many entities get full alias/property detail in the prompt. Every entity NAME is
    # always sent (full recall); only this many also get the heavier alias/property detail.
    ENTITY_DETAIL_TOP_N = 50

    def _truncate(self, text: str, limit: int, note: str = " …[truncated]") -> str:
        text = text or ""
        if len(text) <= limit:
            return text
        return text[: max(0, limit - len(note))] + note

    def _scored_entities(
        self,
        entities: list[Entity],
        prepared: PreparedEmail,
        mappings: list,
        entity_phrase_hints: list[str],
        signals: list[dict] | None = None,
    ) -> list[Entity]:
        # Rank entities by relevance to this email. Ordering only decides which entities get
        # full alias/property detail and the order names are listed in -- it never hides an
        # entity, so the correct one is always available to Claude (no recall loss).
        haystack = " ".join(
            [
                prepared.email.subject or "",
                prepared.email.sender or "",
                prepared.email.body_text or "",
                prepared.text_preview or "",
                " ".join(entity_phrase_hints or []),
            ]
        ).lower()
        mapping_entities = {(m.entity or "").strip().lower() for m in mappings if getattr(m, "entity", None)}
        # Normalized address/org keys from this email. Matching an entity's property/alias on
        # the SAME normalized key bridges "1339 N Front St" <-> "1339 North Front Street LLC",
        # which plain substring containment misses. These are the strongest ranking signals.
        email_addr_keys = {s["value"] for s in (signals or []) if s["type"] == "address"}
        email_org_keys = {s["value"] for s in (signals or []) if s["type"] == "org"}

        def score(entity: Entity) -> int:
            value = 0
            terms = [entity.entity_name, *(entity.aliases or []), *(entity.properties or [])]
            for term in terms:
                if not term:
                    continue
                if term.lower() in haystack:
                    value += 1
                if email_addr_keys and email_addr_keys.intersection(extract_addresses(term)):
                    value += 10  # normalized street address match (number + street)
                if email_org_keys and normalize_org(term) in email_org_keys:
                    value += 8  # normalized LLC / org-name match
            if (entity.entity_name or "").strip().lower() in mapping_entities:
                value += 5  # an entity we have already learned for this email's signals
            return value

        return sorted(entities, key=score, reverse=True)

    def _role_for_contact(self, text: str, name: str) -> tuple[str | None, str | None]:
        # Look at the signature window around each mention of the person for a role keyword.
        if not name or not text:
            return None, None
        lowered = text.lower()
        for match in re.finditer(re.escape(name.lower()), lowered):
            window = lowered[match.start(): match.end() + 220]
            for term in self.COUNTERPARTY_ROLE_TERMS:
                if term in window:
                    return term, "counterparty"
            for term in self.OWNER_ROLE_TERMS:
                if term in window:
                    return term, "owner"
        return None, None

    def _entity_phrase_hints(self, prepared: PreparedEmail) -> list[str]:
        candidates: list[str] = []
        subject = self._clean_subject(prepared.email.subject or "")
        if subject:
            candidates.append(subject)
            for separator in [" - ", " – ", " — ", ":"]:
                if separator in subject:
                    candidates.append(subject.split(separator, 1)[0].strip())
                    break

        text = "\n".join([prepared.email.body_text or "", prepared.text_preview or ""])
        patterns = [
            r"\bfor\s+(?:the\s+)?([A-Z][A-Za-z0-9&.,' -]{4,90}?(?:LLC|LP|Inc\.?|Plaza|Property|Properties|Retreat|Apartments|Holdings))\b",
            r"\b([A-Z][A-Za-z0-9&.,' -]{4,90}?(?:LLC|LP|Inc\.?|Plaza|Property|Properties|Retreat|Apartments|Holdings))\b",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                candidates.append(match.group(1).strip(" .,-"))
                if len(candidates) >= 20:
                    break

        output: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            value = re.sub(r"\s+", " ", value).strip(" .,-")
            key = value.lower()
            if not value or len(value) < 5 or key in seen:
                continue
            if key in {"financials", "statement", "statements", "attached"}:
                continue
            seen.add(key)
            output.append(value)
            if len(output) >= 10:
                break
        return output

    def _clean_contact_name(self, value: str) -> str:
        value = re.sub(r"\([^)]*\)", "", value or "")
        value = re.sub(r"\s+", " ", value).strip(" '\"\t\r\n")
        return value

    def _format_person_name(self, value: str) -> str | None:
        parts = [part for part in re.split(r"\s+", self._clean_contact_name(value)) if part]
        if len(parts) < 2:
            return None
        first = re.sub(r"[^A-Za-z]", "", parts[0])
        last = re.sub(r"[^A-Za-z'-]", "", parts[-1])
        if not first or not last:
            return None
        return f"{first[0].upper()}. {last}"

    def _blocked_principal_candidate(
        self,
        name: str,
        email: str,
        domain: str,
        forwarder_domains: set[str],
    ) -> str | None:
        lowered = " ".join([name, email, domain]).lower()
        if domain in forwarder_domains:
            return "forwarder_or_internal_domain"
        blocked_terms = [
            "file cabinet",
            "no-reply",
            "noreply",
            "notification",
            "support",
            "operations",
            "accounting",
            "treasurer",
            "tax collector",
        ]
        for term in blocked_terms:
            if term in lowered:
                return term.replace(" ", "_")
        return None

    def _clean_subject(self, value: str) -> str:
        value = value or ""
        while True:
            updated = re.sub(r"^\s*(fwd?|re):\s*", "", value, flags=re.I).strip()
            if updated == value.strip():
                return updated
            value = updated

    def _artifact_prompt_payload(self, prepared: PreparedEmail) -> list[dict]:
        output = []
        for artifact in prepared.artifacts:
            if artifact.kind == "combined_package":
                continue
            payload = {
                "kind": artifact.kind,
                "original_filename": artifact.original_filename,
                # First few pages of the attachment — enough to read account/policy numbers
                # and statement-period dates that drive the Level 3 subfolder + filename date.
                "text_preview": (artifact.text_preview or "")[:8000],
                "requires_claude_pdf": artifact.requires_claude_pdf,
            }
            # Mail parser suspects this image is a signature logo/banner kept out of caution.
            # The prompt asks Claude to confirm or deny via a per-attachment "decorative" field.
            if getattr(artifact, "ambiguous_image", False):
                payload["possibly_decorative"] = True
            output.append(payload)
        return output

    def _parse_json_response(self, text: str) -> ClassificationResult:
        data = self._decode_first_json_object(text)
        if not isinstance(data, dict):
            return self._needs_review_parse_failure(f"Claude response did not contain JSON: {text[:200]}")
        confidence = self._normalize_confidence(data.get("confidence", 0))
        action = (data.get("action") or "needs_review").strip().lower()
        needs_review = action == "needs_review"
        unknown_entity = self._parse_bool(data.get("unknown_entity"), default=False)
        summaries, classifications = self._normalize_artifact_summaries(data.get("artifact_summaries"))
        additional_entities = self._normalize_additional_entities(data.get("additional_entities"), data.get("entity"))
        return ClassificationResult(
            entity=data.get("entity"),
            level2=data.get("level2"),
            level3=data.get("level3"),
            file_summary=data.get("file_summary") or "Unclassified Filing Document",
            confidence=confidence,
            unknown_entity=unknown_entity,
            needs_review=needs_review,
            urgent=confidence < self.settings.urgent_review_confidence,
            reason=data.get("reason") or "",
            action=action,
            document_date=data.get("document_date"),
            needs_review_reason=data.get("needs_review_reason"),
            decision_audit={
                "artifact_summaries": summaries,
                "artifact_classifications": classifications,
                "email_sender": data.get("email_sender") or None,
                "additional_entities": additional_entities,
            },
        )

    def _decode_first_json_object(self, text: str) -> dict | None:
        # Claude can occasionally append a second JSON/debug block or brace-rich prose after the
        # primary object. Decode the first valid JSON object and ignore trailing noise.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                parsed, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _normalize_additional_entities(self, raw: object, primary: object) -> list[str]:
        # Other Known entities this email also belongs to. Dedupe, drop blanks, and drop the
        # primary entity if the model echoed it. The decision validator uses this (with the
        # per-attachment entities) to auto-split a confident multi-entity email or route an
        # uncertain one to review.
        if not isinstance(raw, list):
            return []
        primary_norm = (str(primary or "")).strip().lower()
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            name = str(item or "").strip()
            key = name.lower()
            if not name or key == primary_norm or key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out

    def _normalize_artifact_summaries(self, raw: object) -> tuple[dict, dict]:
        # Accept both the legacy string form ({key: "summary"}) and the dual-filing object form
        # ({key: {"summary", "level2", "level3"}}). Returns (summaries, classifications).
        summaries: dict[str, str] = {}
        classifications: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return summaries, classifications
        for key, value in raw.items():
            if isinstance(value, dict):
                summaries[key] = value.get("summary") or value.get("file_summary") or ""
                classifications[key] = {
                    "level2": value.get("level2"),
                    "level3": value.get("level3"),
                    "document_date": value.get("document_date"),
                    # Per-attachment entity for multi-entity splitting. None/blank when the
                    # email is single-entity; resolved against Known entities downstream.
                    "entity": (str(value.get("entity")).strip() or None) if value.get("entity") else None,
                    "entity_confidence": self._normalize_confidence(value.get("entity_confidence"))
                    if value.get("entity_confidence") is not None
                    else None,
                    # Claude's verdict on a possibly_decorative image (signature logo vs real
                    # document). Only honored when the part-classifier ALSO flagged the artifact
                    # (see filing_service.apply_decorative_flags) -- absent/None otherwise.
                    "decorative": value.get("decorative") if isinstance(value.get("decorative"), bool) else None,
                }
            else:
                summaries[key] = value
        return summaries, classifications

    def _needs_review_parse_failure(self, reason: str) -> ClassificationResult:
        return ClassificationResult(
            entity=None,
            level2=None,
            level3=None,
            file_summary="Claude Classification Needs Review",
            confidence=0,
            unknown_entity=True,
            needs_review=True,
            reason=reason,
            action="needs_review",
            needs_review_reason="classification_parse_failure",
            urgent=True,
        )

    def _normalize_confidence(self, value: object) -> float:
        confidence = float(value or 0)
        if 0 < confidence <= 1:
            return confidence * 100
        return confidence

    def _parse_bool(self, value: object, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        return default

    def _mock_classify(self, prepared: PreparedEmail, entities: list[Entity]) -> ClassificationResult:
        text = " ".join(
            [
                prepared.email.subject or "",
                prepared.email.sender or "",
                prepared.text_preview,
            ]
        )
        text_lower = text.lower()
        matched_entity = None
        for entity in entities:
            terms = [entity.entity_name, *(entity.aliases or []), *(entity.properties or [])]
            if any(term and term.lower() in text_lower for term in terms):
                matched_entity = entity.entity_name
                break

        if "insurance" in text_lower or "policy" in text_lower or "umbrella" in text_lower:
            level2 = "Insurance"
        elif "tax collector" in text_lower or "property tax" in text_lower or "parcel" in text_lower:
            level2 = "Property Taxes"
        elif "receipt" in text_lower:
            level2 = "Receipts / Supporting Documents"
        elif "bank statement" in text_lower:
            level2 = "Bank Statements"
        else:
            level2 = "Communications"

        confidence = 90 if matched_entity else 65
        return ClassificationResult(
            entity=matched_entity,
            level2=level2,
            level3=None,
            file_summary=self._mock_summary(prepared, level2, matched_entity),
            confidence=confidence,
            unknown_entity=matched_entity is None,
            needs_review=matched_entity is None or confidence < self.settings.auto_file_confidence or bool(prepared.issues),
            urgent=confidence < self.settings.urgent_review_confidence,
            reason="Mock classifier based on keyword and entity matching.",
            action="file" if matched_entity and confidence >= self.settings.auto_file_confidence and not prepared.issues else "needs_review",
        )

    def _mock_summary(self, prepared: PreparedEmail, level2: str, entity: str | None) -> str:
        subject = prepared.email.subject or "Filing Document"
        subject = re.sub(r"^(fwd:|fw:)\s*", "", subject, flags=re.I).strip()
        if level2 == "Insurance":
            summary = re.sub(r"general liability|gl", "General Liability", subject, flags=re.I)
        elif level2 == "Property Taxes":
            summary = subject.replace("Receipt for", "").strip() or "Property Tax Receipt"
        else:
            summary = subject
        if entity and entity.lower() not in summary.lower():
            summary = f"{summary} for {entity}"
        return summary[:160]
