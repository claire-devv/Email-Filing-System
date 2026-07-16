from sqlalchemy import delete

from app.db.models import FilingLog, ProcessedEmail
from app.db.session import SessionLocal
from app.services.processing_service import ProcessingService
from app.services.usage_guard import ApiLimitReached


def main() -> None:
    service = ProcessingService()
    db = SessionLocal()
    try:
        email = ProcessedEmail(
            gmail_message_id="api-limit-test",
            sender="sender@example.com",
            subject="API Limit Test",
            status="processing",
            metadata_json={},
        )
        db.add(email)
        db.commit()
        db.refresh(email)

        result = service._pause_for_api_limit(
            db,
            email,
            ApiLimitReached(provider="claude", used=3, limit=3),
        )
        assert result["status"] == "waiting_api_limit"
        assert result["email_id"] == email.id
        db.refresh(email)
        assert email.status == "waiting_api_limit"
        assert "daily call limit reached" in (email.last_error or "")
        assert (email.metadata_json or {}).get("retryable_reason") == "api_limit"

        db.execute(delete(FilingLog).where(FilingLog.email_id == email.id))
        db.delete(email)
        db.commit()
    finally:
        db.close()

    print({"api_limit_handling": "ok"})


if __name__ == "__main__":
    main()
