from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FileArtifact, FilingLog, GmailWatchState, NeedsReview, ProcessedEmail, ProcessedFile
from app.db.session import get_db
from app.schemas.common import ActivityItem, ArtifactOut
from app.utils.time import as_utc, utc_now

router = APIRouter(prefix="/activity", tags=["activity"])

FILED_STATUSES = ("filed", "approved", "corrected")


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    # Headline numbers for the dashboard Home: today's volume plus the live review backlog.
    # "Today" is the client's day (display timezone), so the count rolls over at local midnight
    # rather than midnight UTC. The boundary is converted back to UTC for the stored timestamps.
    try:
        tz = ZoneInfo(get_settings().display_timezone)
    except Exception:
        tz = timezone.utc
    today_start = (
        datetime.now(tz)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )

    def _count(*conditions) -> int:
        return db.scalar(
            select(func.count(func.distinct(FilingLog.email_id)))
            .select_from(FilingLog)
            .where(FilingLog.created_at >= today_start, *conditions)
        ) or 0

    pending = db.scalar(
        select(func.count()).select_from(NeedsReview).where(NeedsReview.status == "pending")
    ) or 0

    # Count emails currently mid-processing (background tasks in-flight).
    processing_count = db.scalar(
        select(func.count()).select_from(ProcessedEmail).where(ProcessedEmail.status == "processing")
    ) or 0

    # Watch health: surface inline so the dashboard needs only one poll per refresh cycle.
    watch_state = db.execute(
        select(GmailWatchState)
        .where(GmailWatchState.active.is_(True))
        .order_by(GmailWatchState.updated_at.desc())
    ).scalars().first()

    watch_active = bool(watch_state and watch_state.active)
    watch_error = watch_state.last_error if watch_state else None
    last_sync_ago_minutes: int | None = None
    if watch_state and watch_state.last_successful_sync_at:
        delta = utc_now() - as_utc(watch_state.last_successful_sync_at)
        last_sync_ago_minutes = int(delta.total_seconds() // 60)

    return {
        "processed_today": _count(),
        "filed_today": _count(FilingLog.status.in_(FILED_STATUSES)),
        "errors_today": _count(FilingLog.status == "failed"),
        "needs_review_pending": pending,
        "processing_count": processing_count,
        "watch_active": watch_active,
        "watch_error": watch_error,
        "last_sync_ago_minutes": last_sync_ago_minutes,
    }


@router.get("", response_model=list[ActivityItem])
def list_activity(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)) -> list[ActivityItem]:
    logs = db.execute(
        select(FilingLog)
        .order_by(FilingLog.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .offset(max(0, offset))
    ).scalars().all()

    # Batch-load everything this page needs in 4 queries instead of 3×N.
    email_ids = list({log.email_id for log in logs if log.email_id})
    if email_ids:
        artifacts_by_email: dict[int, list[FileArtifact]] = {}
        for a in db.execute(
            select(FileArtifact).where(FileArtifact.email_id.in_(email_ids))
        ).scalars().all():
            artifacts_by_email.setdefault(a.email_id, []).append(a)

        emails_by_id: dict[int, ProcessedEmail] = {
            e.id: e
            for e in db.execute(
                select(ProcessedEmail).where(ProcessedEmail.id.in_(email_ids))
            ).scalars().all()
        }

        # Latest NeedsReview per email — used only when ProcessedEmail lacks a decision_audit.
        # Order desc so setdefault keeps the first (= latest) per email_id.
        reviews_by_email: dict[int, NeedsReview] = {}
        for r in db.execute(
            select(NeedsReview)
            .where(NeedsReview.email_id.in_(email_ids))
            .order_by(NeedsReview.created_at.desc())
        ).scalars().all():
            reviews_by_email.setdefault(r.email_id, r)
    else:
        artifacts_by_email, emails_by_id, reviews_by_email = {}, {}, {}

    rows: list[tuple] = []
    drive_file_ids: set[str] = set()
    for log in logs:
        artifacts = artifacts_by_email.get(log.email_id, []) if log.email_id else []
        decision_audit = {}
        processing_metadata = {}
        received_at = None
        source = "email"
        if log.email_id:
            email = emails_by_id.get(log.email_id)
            if email:
                received_at = email.received_at
                metadata = email.metadata_json or {}
                source = (metadata.get("upload") or {}).get("source_kind") or "email"
                decision_audit = metadata.get("decision_audit") or {}
                processing_metadata = {
                    key: metadata.get(key)
                    for key in [
                        "real_attachment_count",
                        "inline_asset_count",
                        "ignored_inline_assets",
                        "inline_assets",
                        "ambiguous_part_count",
                        "part_classifications",
                    ]
                    if key in metadata
                }
            if not decision_audit:
                review = reviews_by_email.get(log.email_id)
                if review:
                    decision_audit = (review.metadata_json or {}).get("decision_audit") or {}
        drive_file_ids.update(a.drive_file_id for a in artifacts if a.drive_file_id)
        rows.append((log, artifacts, decision_audit, processing_metadata, received_at, source))

    # Names the files actually carry in Drive (one batch lookup for the whole page).
    drive_names: dict[str, str] = {}
    if drive_file_ids:
        drive_names = dict(
            db.execute(
                select(ProcessedFile.drive_file_id, ProcessedFile.filename).where(
                    ProcessedFile.drive_file_id.in_(drive_file_ids)
                )
            ).all()
        )

    output: list[ActivityItem] = []
    for log, artifacts, decision_audit, processing_metadata, received_at, source in rows:
        row_artifacts = _row_artifacts(log, artifacts)
        artifacts_out = []
        for a in row_artifacts:
            out = ArtifactOut.model_validate(a)
            out.drive_filename = drive_names.get(a.drive_file_id) if a.drive_file_id else None
            out.decorative = bool((a.metadata_json or {}).get("decorative"))
            artifacts_out.append(out)
        output.append(
            ActivityItem.model_validate(
                {
                    **log.__dict__,
                    "received_at": received_at,
                    "folder_drive_id": _folder_drive_id(log, artifacts),
                    "artifacts": artifacts_out,
                    "decision_audit": decision_audit,
                    "processing_metadata": processing_metadata,
                    "source": source,
                }
            )
        )
    return output


def _path_segments(path: str | None) -> list[str]:
    # Drive paths are "Root / Entity / Level2 / Level3" (folder names can't contain "/").
    return [seg.strip() for seg in (path or "").split("/") if seg.strip()]


def _row_artifacts(log: FilingLog, artifacts: list[FileArtifact]) -> list[FileArtifact]:
    # On a multi-entity split each FilingLog is one entity, so a row should list only the
    # attachments that landed in THAT entity's folders, plus the combined email PDF (it is copied
    # into every involved entity's Communications). Single-entity emails are unaffected: all of
    # their attachments carry that one entity in their stamped path, so all are kept.
    real = [a for a in artifacts if a.kind not in ("combined_package", "email_body")]
    combined = [a for a in artifacts if a.kind == "combined_package"]
    entity = (log.entity or "").strip()
    matched = [
        a for a in real if entity and entity in _path_segments((a.metadata_json or {}).get("folder_path"))
    ]
    # Fallback: legacy rows without a stamped path (or no entity) -> show every attachment.
    if not matched:
        matched = real
    return matched + combined


def _folder_drive_id(log: FilingLog, artifacts: list[FileArtifact]) -> str | None:
    # The Drive folder this row actually filed into. On a multi-entity split each FilingLog is a
    # different entity, so prefer the attachment whose stamped folder_path matches THIS row's
    # folder_path -- that gives a per-entity folder link instead of always the first attachment's.
    for a in artifacts:
        if a.kind == "combined_package" or not a.drive_folder_id:
            continue
        if log.folder_path and (a.metadata_json or {}).get("folder_path") == log.folder_path:
            return a.drive_folder_id
    # Fallback (single-entity / legacy rows): first real attachment's folder, then any.
    primary = next((a for a in artifacts if a.drive_folder_id and a.kind != "combined_package"), None) or next(
        (a for a in artifacts if a.drive_folder_id), None
    )
    return primary.drive_folder_id if primary else None


@router.get("/{activity_id}/files", response_model=list[ArtifactOut])
def activity_files(activity_id: int, db: Session = Depends(get_db)) -> list[FileArtifact]:
    log = db.get(FilingLog, activity_id)
    if not log:
        raise HTTPException(status_code=404, detail="Activity item not found.")
    if not log.email_id:
        return []
    return db.execute(select(FileArtifact).where(FileArtifact.email_id == log.email_id)).scalars().all()
