from pydantic import BaseModel

from app.schemas.common import ORMModel, UtcDateTime


class EntityOut(ORMModel):
    id: int
    entity_name: str
    folder_name: str
    drive_folder_id: str | None = None
    aliases: list
    properties: list
    active: bool
    last_used_at: UtcDateTime | None = None
    documents_filed: int = 0


class EntityCreate(BaseModel):
    entity_name: str
    aliases: list[str] = []
    properties: list[str] = []
    drive_folder_id: str | None = None


class EntityUpdate(BaseModel):
    aliases: list[str] | None = None
    active: bool | None = None
