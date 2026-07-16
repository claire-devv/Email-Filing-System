from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.email import ProcessEmailResponse, ProcessUnreadRequest, ProcessUnreadResponse
from app.services.processing_service import ProcessingService

router = APIRouter(prefix="/emails", tags=["emails"])


@router.post("/{gmail_message_id}/process", response_model=ProcessEmailResponse)
def process_email(gmail_message_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return ProcessingService().process_message(db, gmail_message_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/process-unread", response_model=ProcessUnreadResponse)
def process_unread(payload: ProcessUnreadRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return ProcessingService().process_unread(db, payload.limit, payload.newer_than_minutes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
