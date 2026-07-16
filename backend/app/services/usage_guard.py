from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ApiUsage


class ApiLimitReached(RuntimeError):
    def __init__(self, provider: str, used: int, limit: int) -> None:
        self.provider = provider
        self.used = used
        self.limit = limit
        super().__init__(
            f"{provider} daily call limit reached: {used}/{limit}. "
            "Increase the env limit only after confirming this is intentional."
        )


class ApiUsageGuard:
    def assert_available(self, db: Session, provider: str, daily_limit: int, increment: int = 1) -> ApiUsage:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usage = db.execute(
            select(ApiUsage).where(ApiUsage.provider == provider, ApiUsage.usage_date == today)
        ).scalars().first()
        if not usage:
            usage = ApiUsage(provider=provider, usage_date=today, call_count=0)
            db.add(usage)
            db.flush()
        if usage.call_count + increment > daily_limit:
            raise ApiLimitReached(provider=provider, used=usage.call_count, limit=daily_limit)
        return usage

    def increment(self, db: Session, provider: str, daily_limit: int, increment: int = 1) -> ApiUsage:
        usage = self.assert_available(db, provider, daily_limit, increment)
        usage.call_count += increment
        db.commit()
        db.refresh(usage)
        return usage
