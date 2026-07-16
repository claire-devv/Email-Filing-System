import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from googleapiclient.discovery import build
from PIL import Image

from app.core.config import get_settings
from app.services.google_auth import get_user_credentials
from app.services.types import EmailAttachment, EmailMessageData, InlineAsset
from app.utils.files import ensure_dir, safe_filename


@dataclass(frozen=True)
class PartClassification:
    kind: str
    reason: str
    issue: str | None = None
    width: int | None = None
    height: int | None = None


class GmailService:
    # Process-level label-ID cache: avoids a labels().list() API call on every
    # mark_filed / mark_failed / mark_skipped. Reset on process restart.
    _label_id_cache: dict[str, str] = {}

    def __init__(self) -> None:
        self.settings = get_settings()
        self._service = None

    @property
    def service(self):
        if not self.settings.enable_real_google:
            raise RuntimeError("ENABLE_REAL_GOOGLE=true is required before calling Gmail/Drive APIs.")
        if self._service is None:
            self._service = build("gmail", "v1", credentials=get_user_credentials(), cache_discovery=False)
        return self._service

    def search_unread(self, limit: int = 20, newer_than_minutes: int | None = None) -> list[str]:
        limit = min(limit, self.settings.process_unread_max_limit)
        query = f"is:unread -label:{self.settings.gmail_filed_label} -label:{self.settings.gmail_failed_label}"
        if newer_than_minutes:
            # Gmail search supports newer_than as day granularity only; app-level idempotency handles overlap.
            query += " newer_than:2d"
        response = self.service.users().messages().list(userId=self.settings.gmail_user_id, q=query, maxResults=limit).execute()
        return [item["id"] for item in response.get("messages", [])]

    def get_profile(self) -> dict:
        return self.service.users().getProfile(userId=self.settings.gmail_user_id).execute()

    def start_watch(
        self,
        *,
        topic_name: str,
        label_ids: list[str] | None = None,
        label_filter_behavior: str | None = None,
    ) -> dict:
        body = {
            "topicName": topic_name,
            "labelIds": label_ids or self.settings.gmail_pubsub_label_ids,
            "labelFilterBehavior": label_filter_behavior or self.settings.gmail_pubsub_label_filter_behavior,
        }
        return self.service.users().watch(userId=self.settings.gmail_user_id, body=body).execute()

    def stop_watch(self) -> None:
        self.service.users().stop(userId=self.settings.gmail_user_id).execute()

    def history_message_ids(self, start_history_id: str, label_ids: list[str] | None = None) -> tuple[list[str], str | None]:
        message_ids: list[str] = []
        seen: set[str] = set()
        page_token: str | None = None
        latest_history_id: str | None = None
        required_labels = set(label_ids or self.settings.gmail_pubsub_label_ids or [])
        while True:
            kwargs: dict = {
                "userId": self.settings.gmail_user_id,
                "startHistoryId": str(start_history_id),
                "historyTypes": ["messageAdded"],
            }
            # Let Gmail pre-filter by label server-side — avoids fetching unrelated history.
            if required_labels:
                kwargs["labelId"] = next(iter(required_labels))
            if page_token:
                kwargs["pageToken"] = page_token
            response = self.service.users().history().list(**kwargs).execute()
            latest_history_id = response.get("historyId") or latest_history_id
            for history in response.get("history", []) or []:
                latest_history_id = history.get("id") or latest_history_id
                for item in history.get("messagesAdded", []) or []:
                    message = item.get("message") or {}
                    message_id = message.get("id")
                    labels = set(message.get("labelIds") or [])
                    if not message_id or message_id in seen:
                        continue
                    # Client-side label check: if labels are known and none match, skip.
                    # If labels are absent (empty), include — Gmail may omit them in history.
                    if required_labels and labels and not required_labels.intersection(labels):
                        continue
                    seen.add(message_id)
                    message_ids.append(message_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                return message_ids, latest_history_id

    def fetch_message(self, gmail_message_id: str, artifact_dir: Path) -> EmailMessageData:
        message = self.service.users().messages().get(
            userId=self.settings.gmail_user_id, id=gmail_message_id, format="full"
        ).execute()
        payload = message.get("payload", {})
        headers = {item["name"].lower(): item.get("value", "") for item in payload.get("headers", [])}
        body_text, body_html = self._extract_body(payload)
        attachments, inline_assets, attachment_metadata = self._download_message_files(
            gmail_message_id,
            payload,
            artifact_dir,
            body_html,
        )
        received = self._received_at(headers, message.get("internalDate"))
        return EmailMessageData(
            gmail_message_id=gmail_message_id,
            thread_id=message.get("threadId"),
            sender=headers.get("from"),
            recipient=headers.get("to"),
            cc=headers.get("cc"),
            subject=headers.get("subject"),
            received_at=received,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            inline_assets=inline_assets,
            raw_metadata={"headers": headers, "labelIds": message.get("labelIds", []), **attachment_metadata},
        )

    def mark_filed(self, gmail_message_id: str) -> None:
        self._add_label_and_mark_read(gmail_message_id, self.settings.gmail_filed_label)

    def mark_failed(self, gmail_message_id: str) -> None:
        self._add_label(gmail_message_id, self.settings.gmail_failed_label)

    def mark_skipped(self, gmail_message_id: str) -> None:
        self._add_label_and_mark_read(gmail_message_id, self.settings.gmail_skipped_label)

    def _add_label_and_mark_read(self, gmail_message_id: str, label_name: str) -> None:
        label_id = self._ensure_label(label_name)
        self.service.users().messages().modify(
            userId=self.settings.gmail_user_id,
            id=gmail_message_id,
            body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
        ).execute()

    def _add_label(self, gmail_message_id: str, label_name: str) -> None:
        label_id = self._ensure_label(label_name)
        self.service.users().messages().modify(
            userId=self.settings.gmail_user_id,
            id=gmail_message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def _ensure_label(self, label_name: str) -> str:
        if label_name in GmailService._label_id_cache:
            return GmailService._label_id_cache[label_name]
        labels = self.service.users().labels().list(userId=self.settings.gmail_user_id).execute().get("labels", [])
        for item in labels:
            GmailService._label_id_cache[item["name"]] = item["id"]
        if label_name in GmailService._label_id_cache:
            return GmailService._label_id_cache[label_name]
        created = self.service.users().labels().create(
            userId=self.settings.gmail_user_id,
            body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        GmailService._label_id_cache[label_name] = created["id"]
        return created["id"]

    def _download_message_files(
        self,
        message_id: str,
        payload: dict[str, Any],
        artifact_dir: Path,
        body_html: str | None,
    ) -> tuple[list[EmailAttachment], list[InlineAsset], dict]:
        attachments: list[EmailAttachment] = []
        inline_assets: list[InlineAsset] = []
        ignored_inline_assets: list[str] = []
        part_classifications: list[dict[str, Any]] = []
        ambiguous_part_count = 0
        for part in self._walk_parts(payload):
            filename = part.get("filename")
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")
            if not filename or not attachment_id:
                continue
            data = self.service.users().messages().attachments().get(
                userId=self.settings.gmail_user_id, messageId=message_id, id=attachment_id
            ).execute()
            raw = base64.urlsafe_b64decode(data.get("data", "").encode("utf-8"))
            mime_type = part.get("mimeType")
            content_id = self._header(part, "Content-ID")
            disposition = self._header(part, "Content-Disposition")
            dimensions = self._image_dimensions_from_bytes(raw, mime_type)
            classification = self._classify_part(
                filename=filename,
                mime_type=mime_type,
                size_bytes=len(raw),
                content_id=content_id,
                disposition=disposition,
                body_html=body_html,
                dimensions=dimensions,
            )
            part_classifications.append(
                {
                    "filename": filename,
                    "mime_type": mime_type,
                    "content_id": self._clean_content_id(content_id),
                    "disposition": disposition,
                    "classification": classification.kind,
                    "reason": classification.reason,
                    "issue": classification.issue,
                    "size_bytes": len(raw),
                    "width": classification.width,
                    "height": classification.height,
                }
            )
            if classification.issue:
                ambiguous_part_count += 1
            if classification.kind == "inline_asset":
                path = ensure_dir(artifact_dir / "inline") / safe_filename(filename)
                path.write_bytes(raw)
                inline_assets.append(
                    InlineAsset(
                        filename=filename,
                        mime_type=mime_type,
                        local_path=path,
                        size_bytes=len(raw),
                        content_id=self._clean_content_id(content_id),
                        width=classification.width,
                        height=classification.height,
                        part_classification_reason=classification.reason,
                    )
                )
                ignored_inline_assets.append(filename)
                continue
            path = ensure_dir(artifact_dir / "original") / safe_filename(filename)
            path.write_bytes(raw)
            attachments.append(
                EmailAttachment(
                    filename=filename,
                    mime_type=mime_type,
                    local_path=path,
                    size_bytes=len(raw),
                    content_id=self._clean_content_id(content_id),
                    part_classification_reason=classification.reason,
                    part_classification_issue=classification.issue,
                )
            )
        return attachments, inline_assets, {
            "real_attachment_count": len(attachments),
            "inline_asset_count": len(inline_assets),
            "ignored_inline_assets": ignored_inline_assets,
            "ambiguous_part_count": ambiguous_part_count,
            "part_classifications": part_classifications,
        }

    def _extract_body(self, payload: dict[str, Any]) -> tuple[str, str | None]:
        text_parts: list[str] = []
        html_parts: list[str] = []
        for part in self._walk_parts(payload):
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if not data:
                continue
            try:
                decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
            except Exception:
                continue
            if mime == "text/plain":
                text_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        return "\n\n".join(text_parts).strip(), "\n\n".join(html_parts).strip() or None

    def _walk_parts(self, payload: dict[str, Any]):
        yield payload
        for part in payload.get("parts", []) or []:
            yield from self._walk_parts(part)

    def _header(self, part: dict[str, Any], name: str) -> str | None:
        for header in part.get("headers", []) or []:
            if header.get("name", "").lower() == name.lower():
                return header.get("value")
        return None

    def _clean_content_id(self, content_id: str | None) -> str | None:
        if not content_id:
            return None
        return unquote(content_id.strip().strip("<>").strip())

    def _cid_key(self, value: str | None) -> str | None:
        cleaned = self._clean_content_id(value)
        if not cleaned:
            return None
        return cleaned.lower()

    def _html_cid_references(self, body_html: str | None) -> set[str]:
        if not body_html:
            return set()
        references: set[str] = set()
        for match in re.finditer(r"cid:([^\"'\s>)]+)", body_html, flags=re.IGNORECASE):
            key = self._cid_key(match.group(1))
            if key:
                references.add(key)
        return references

    def _content_id_variants(self, content_id: str | None) -> set[str]:
        clean_cid = self._clean_content_id(content_id)
        if not clean_cid:
            return set()
        variants = {clean_cid}
        if "@" in clean_cid:
            variants.add(clean_cid.split("@", 1)[0])
        return {key for value in variants if (key := self._cid_key(value))}

    def _cid_referenced_in_html(self, content_id: str | None, body_html: str | None) -> bool:
        references = self._html_cid_references(body_html)
        if not references:
            return False
        for candidate in self._content_id_variants(content_id):
            if candidate in references:
                return True
            if len(candidate) >= 5 and any(ref.startswith(candidate) or candidate.startswith(ref) for ref in references):
                return True
        return False

    def _classify_part(
        self,
        filename: str,
        mime_type: str | None,
        size_bytes: int,
        content_id: str | None,
        disposition: str | None,
        body_html: str | None,
        dimensions: tuple[int | None, int | None] = (None, None),
    ) -> PartClassification:
        disposition_kind = self._disposition_kind(disposition)
        clean_cid = self._clean_content_id(content_id)
        width, height = dimensions
        is_image = (mime_type or "").lower().startswith("image/")

        if clean_cid and self._cid_referenced_in_html(clean_cid, body_html):
            # A cid-referenced image is inline DECORATION (signature, logo, Outlook "imageNNN"
            # body image) only when it is non-image, signature-sized, or carries a generic
            # auto-name. A descriptively-named, document-sized cid image is a document the
            # sender embedded (e.g. an iPhone "Screenshot ....png" attached in Mail) -- keep it
            # as a real attachment so it files to its own category, flagged ambiguous so a human
            # confirms in review rather than the document being buried in Communications.
            if (
                not is_image
                or self._signature_like_metrics(size_bytes, dimensions)
                or self._generic_image_name(filename)
            ):
                return PartClassification(
                    kind="inline_asset",
                    reason="Content-ID is referenced by cid in HTML body (decorative inline image).",
                    width=width,
                    height=height,
                )
            return PartClassification(
                kind="real_attachment",
                reason="Descriptive, document-sized cid-referenced image -- embedded document attachment.",
                issue="ambiguous_image_part",
                width=width,
                height=height,
            )
        if is_image and self._word_temp_image_name(filename):
            # ~WRD####.jpg are Microsoft Word temporary embedded-image artifacts; never real attachments.
            return PartClassification(
                kind="inline_asset",
                reason="Microsoft Word temporary embedded-image artifact.",
                width=width,
                height=height,
            )
        if is_image and clean_cid and self._signature_like_metrics(size_bytes, dimensions):
            # Gmail/Outlook forwards keep a Content-ID on inline logos/signatures but set
            # disposition=attachment and drop the HTML cid reference. An image that carries a
            # Content-ID and has signature-like size/dimensions is inline decoration, not a
            # real document attachment.
            return PartClassification(
                kind="inline_asset",
                reason="Image has a Content-ID and signature-like size/dimensions (inline decoration).",
                width=width,
                height=height,
            )
        if disposition_kind == "attachment":
            return PartClassification(
                kind="real_attachment",
                reason="Explicit attachment disposition with no matching cid reference.",
                width=width,
                height=height,
            )
        if disposition_kind == "inline":
            return PartClassification(
                kind="inline_asset",
                reason="Inline disposition with no matching cid reference.",
                width=width,
                height=height,
            )
        if not is_image:
            return PartClassification(
                kind="real_attachment",
                reason="Non-image part with filename and attachment data.",
                width=width,
                height=height,
            )
        if self._signature_like_image(filename, size_bytes, dimensions):
            return PartClassification(
                kind="inline_asset",
                reason="Generic image part has signature-like size and dimensions.",
                width=width,
                height=height,
            )
        if self._generic_image_name(filename):
            return PartClassification(
                kind="real_attachment",
                reason="Generic image part has no decisive inline signal; retained for review.",
                issue="ambiguous_image_part",
                width=width,
                height=height,
            )
        return PartClassification(
            kind="real_attachment",
            reason="Image filename does not look like a generated inline signature asset.",
            width=width,
            height=height,
        )

    def _is_inline_asset(
        self,
        filename: str,
        mime_type: str | None,
        size_bytes: int,
        content_id: str | None,
        disposition: str | None,
        body_html: str | None,
    ) -> tuple[bool, tuple[int | None, int | None]]:
        classification = self._classify_part(
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            content_id=content_id,
            disposition=disposition,
            body_html=body_html,
        )
        return classification.kind == "inline_asset", (classification.width, classification.height)

    def _disposition_kind(self, disposition: str | None) -> str | None:
        if not disposition:
            return None
        return disposition.split(";", 1)[0].strip().lower() or None

    def _generic_image_name(self, filename: str) -> bool:
        return bool(re.match(r"image\d{3,}\.(png|jpe?g|gif|webp|bmp)$", filename.lower()))

    def _word_temp_image_name(self, filename: str) -> bool:
        return bool(re.match(r"~wrd\d+\.(png|jpe?g|gif|webp|bmp)$", filename.lower()))

    def _signature_like_metrics(self, size_bytes: int, dimensions: tuple[int | None, int | None]) -> bool:
        # Logos/signatures/banners are physically small (one dimension short) or tiny in bytes.
        width, height = dimensions
        if width is not None and height is not None:
            return width <= 700 and height <= 300
        return size_bytes <= 50 * 1024

    def _signature_like_image(
        self,
        filename: str,
        size_bytes: int,
        dimensions: tuple[int | None, int | None] = (None, None),
    ) -> bool:
        if not self._generic_image_name(filename):
            return False
        if size_bytes > 100 * 1024:
            return False
        width, height = dimensions
        if width is not None and height is not None:
            return width <= 600 and height <= 200
        return size_bytes <= 25 * 1024

    def _image_dimensions_from_bytes(self, raw: bytes, mime_type: str | None) -> tuple[int | None, int | None]:
        if not (mime_type or "").lower().startswith("image/"):
            return None, None
        try:
            with Image.open(BytesIO(raw)) as image:
                return image.width, image.height
        except Exception:
            return None, None

    def _image_dimensions(self, path: Path) -> tuple[int | None, int | None]:
        try:
            with Image.open(path) as image:
                return image.width, image.height
        except Exception:
            return None, None

    def _received_at(self, headers: dict[str, str], internal_date: str | None) -> datetime | None:
        # Always return an aware UTC datetime. The Date: header carries the sender's local
        # offset (e.g. -0400); normalizing to UTC here keeps received_at on the same clock as
        # every other timestamp (created_at/updated_at), so the dashboard never shows a mix of
        # timezones. SQLite drops tzinfo on write, so the value read back is naive-UTC.
        if headers.get("date"):
            try:
                parsed = parsedate_to_datetime(headers["date"])
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except Exception:
                pass
        if internal_date:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        return None
