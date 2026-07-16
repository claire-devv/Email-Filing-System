"""
Show what the upload scanner actually READ from each uploaded file (proof it reads content, not
just the filename). For every recent drive-upload row it prints: the original filename, the
extracted text preview, whether the file was sent to Claude's vision, and the resulting
classification (entity / Level-2 / Level-3 / summary).

Run from backend/:  .venv\\Scripts\\python.exe -m app.scripts.inspect_upload_scan
"""
from sqlalchemy import select

from app.db.models import FileArtifact, FilingLog, ProcessedEmail
from app.db.session import SessionLocal


def main() -> None:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(ProcessedEmail)
            .where(ProcessedEmail.gmail_message_id.like("drive-upload:%"))
            .order_by(ProcessedEmail.id.desc())
            .limit(20)
        ).scalars().all()

        if not rows:
            print("No drive-upload rows found yet. Run run_upload_scan_once first.")
            return

        for email in rows:
            upload = (email.metadata_json or {}).get("upload") or {}
            audit = (email.metadata_json or {}).get("decision_audit") or {}
            print("=" * 90)
            print(f"FILE:    {upload.get('original_filename')!r}   (source: {upload.get('source_kind')})")
            print(f"STATUS:  {email.status}")
            if email.last_error:
                print(f"ERROR:   {email.last_error}")

            artifacts = db.execute(
                select(FileArtifact).where(FileArtifact.email_id == email.id)
            ).scalars().all()
            attach = next((a for a in artifacts if a.kind == "attachment"), None)
            if attach:
                meta = attach.metadata_json or {}
                preview = (meta.get("text_preview") or "").strip()
                sent_to_vision = meta.get("requires_claude_pdf")
                print(f"READ AS: {'Claude VISION (image of the document)' if sent_to_vision else 'extracted TEXT'}")
                print(f"CONTENT THE SYSTEM READ (first 600 chars of {len(preview)} total):")
                print("  " + (preview[:600].replace(chr(10), chr(10) + '  ') if preview else "(no extractable text -- relied on vision)"))
            else:
                print("READ AS: (no attachment artifact -- file was unsupported/oversized)")

            # Where it actually filed: the FilingLog is the source of truth for an upload (the
            # upload filing path doesn't write metadata_json["final"]).
            log = db.execute(
                select(FilingLog).where(FilingLog.email_id == email.id).order_by(FilingLog.id.desc())
            ).scalars().first()
            print("FILED TO:")
            print(f"  entity = {(log.entity if log else None)!r}")
            print(f"  folder = {(log.folder_path if log else None)!r}")
            summaries = audit.get("artifact_summaries") or {}
            for key, val in summaries.items():
                if key == "email_body":
                    continue
                if isinstance(val, dict):
                    print(f"  level2={val.get('level2')!r} level3={val.get('level3')!r} summary={val.get('summary')!r}")
                else:
                    print(f"  summary={val!r}")
            print()
    finally:
        db.close()


if __name__ == "__main__":
    main()
