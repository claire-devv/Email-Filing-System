from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import NeedsReview
from app.db.session import get_db

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/counts")
def counts(db: Session = Depends(get_db)) -> dict:
    pending = db.scalar(select(func.count()).select_from(NeedsReview).where(NeedsReview.status == "pending")) or 0
    urgent = db.scalar(
        select(func.count()).select_from(NeedsReview).where(NeedsReview.status == "pending", NeedsReview.urgent.is_(True))
    ) or 0
    return {"pending_review_count": pending, "urgent_review_count": urgent}
