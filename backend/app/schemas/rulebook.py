from typing import Any

from pydantic import BaseModel


class FolderRulebookOut(BaseModel):
    version: str
    rules: dict[str, Any]


class FolderRulebookUpdate(BaseModel):
    rules: dict[str, Any]
