import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Entity, FilingLog
from app.db.session import get_db
from app.schemas.entity import EntityCreate, EntityOut, EntityUpdate
from app.services.drive_service import DriveService
from app.services.entity_service import EntityService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/entities", tags=["entities"])


def _with_last_used(db: Session, entities: list[Entity]) -> list[EntityOut]:
    # Per-entity filing stats for the Entities page: most recent filing ("last used")
    # and how many distinct emails have been filed there ("documents filed").
    rows = db.execute(
        select(
            FilingLog.entity,
            func.max(FilingLog.created_at),
            func.count(func.distinct(FilingLog.email_id)),
        )
        .where(FilingLog.entity.is_not(None))
        .group_by(FilingLog.entity)
    ).all()
    stats = {name: (last_used, documents) for name, last_used, documents in rows}
    out: list[EntityOut] = []
    for entity in entities:
        last_used, documents = stats.get(entity.entity_name, (None, 0))
        out.append(
            EntityOut.model_validate(entity).model_copy(
                update={"last_used_at": last_used, "documents_filed": documents}
            )
        )
    return out


@router.get("", response_model=list[EntityOut])
def list_entities(db: Session = Depends(get_db)):
    return _with_last_used(db, EntityService().list_active(db))


@router.get("/search", response_model=list[EntityOut])
def search_entities(q: str = "", limit: int = 20, db: Session = Depends(get_db)):
    return _with_last_used(db, EntityService().search(db, q, limit))


@router.post("", response_model=EntityOut)
def create_entity(payload: EntityCreate, db: Session = Depends(get_db)):
    name = (payload.entity_name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Entity name is required.")
    service = EntityService()
    if service.get_by_name(db, name):
        raise HTTPException(status_code=409, detail=f"An entity named '{name}' already exists.")

    settings = get_settings()
    # Create the real Drive master folder + the standard Level-2 set, then register it. When Drive
    # isn't wired (local/mock), just register the row -- the folder is created lazily on first filing.
    if settings.enable_real_google and settings.drive_root_id:
        try:
            entity = DriveService().ensure_entity_folder(db, name)
        except Exception as exc:
            logger.exception("Failed to create Drive folder for new entity %r", name)
            raise HTTPException(status_code=502, detail=f"Could not create the Drive folder: {exc}") from exc
        # Seed the same name-derived aliases an imported folder gets (e.g. "123 Street LLC" from
        # "J. Doe - 123 Street LLC"), plus anything the caller supplied.
        derived = service._aliases_from_folder_name(name)
        entity = service.update(db, entity.id, aliases=[*derived, *(payload.aliases or [])])
        return _with_last_used(db, [entity])[0]

    entity = service.create(db, name, payload.aliases, payload.properties, payload.drive_folder_id)
    return _with_last_used(db, [entity])[0]


@router.patch("/{entity_id}", response_model=EntityOut)
def update_entity(entity_id: int, payload: EntityUpdate, db: Session = Depends(get_db)):
    entity = EntityService().update(db, entity_id, payload.aliases, payload.active)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found.")
    return _with_last_used(db, [entity])[0]
