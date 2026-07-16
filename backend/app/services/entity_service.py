from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Entity


class EntityService:
    def list_active(self, db: Session) -> list[Entity]:
        return db.execute(select(Entity).where(Entity.active.is_(True)).order_by(Entity.entity_name)).scalars().all()

    def search(self, db: Session, query: str | None, limit: int = 20) -> list[Entity]:
        # Server-side typeahead for the master-folder picker. Filters by name in SQL with a
        # capped result set so it stays fast with hundreds/thousands of client entities,
        # rather than shipping the whole registry to the browser to filter client-side.
        stmt = select(Entity).where(Entity.active.is_(True))
        term = (query or "").strip()
        if term:
            stmt = stmt.where(Entity.entity_name.ilike(f"%{term}%"))
        stmt = stmt.order_by(Entity.entity_name).limit(max(1, min(limit, 50)))
        return db.execute(stmt).scalars().all()

    def get_by_name(self, db: Session, entity_name: str) -> Entity | None:
        return db.execute(select(Entity).where(Entity.entity_name == entity_name)).scalars().first()

    def create(self, db: Session, entity_name: str, aliases: list[str] | None = None, properties: list[str] | None = None,
               drive_folder_id: str | None = None) -> Entity:
        existing = self.get_by_name(db, entity_name)
        if existing:
            return existing
        entity = Entity(
            entity_name=entity_name,
            folder_name=entity_name,
            aliases=aliases or [],
            properties=properties or [],
            drive_folder_id=drive_folder_id,
            active=True,
        )
        db.add(entity)
        db.commit()
        db.refresh(entity)
        return entity

    def import_entities(self, db: Session, folders: list[dict]) -> dict:
        # Upsert the master client folders as entities (the entity source of truth).
        # Existing rows keep their learned aliases/properties; we refresh drive_folder_id,
        # reactivate, and merge in name-derived alias variants. Returns created/updated counts.
        created = 0
        updated = 0
        duplicates = 0
        seen_names: set[str] = set()
        for folder in folders:
            name = (folder.get("name") or "").strip()
            if not name:
                continue
            if name in seen_names:
                # Google Drive allows two top-level folders with the same name (different IDs),
                # but entity_name is UNIQUE. Keep the first occurrence and skip the rest instead
                # of crashing the whole import on the UNIQUE constraint. Rename one folder in
                # Drive if both are really distinct clients.
                duplicates += 1
                continue
            seen_names.add(name)
            drive_folder_id = folder.get("id")
            generated_aliases = self._aliases_from_folder_name(name)
            existing = self.get_by_name(db, name)
            if existing:
                changed = False
                if drive_folder_id and existing.drive_folder_id != drive_folder_id:
                    existing.drive_folder_id = drive_folder_id
                    changed = True
                aliases = self._merge_aliases(existing.aliases or [], generated_aliases)
                if aliases != (existing.aliases or []):
                    existing.aliases = aliases
                    changed = True
                if not existing.active:
                    existing.active = True
                    changed = True
                if changed:
                    db.add(existing)
                    updated += 1
            else:
                db.add(
                    Entity(
                        entity_name=name,
                        folder_name=name,
                        drive_folder_id=drive_folder_id,
                        aliases=generated_aliases,
                        properties=[],
                        active=True,
                    )
                )
                created += 1
        # The master folders are the only source of truth. Deactivate entities whose folder
        # is no longer present so Claude never matches a phantom entity. Guard against an empty
        # listing (a Drive API hiccup) so we never deactivate the whole registry by accident.
        deactivated = 0
        if seen_names:
            stale = db.execute(
                select(Entity).where(Entity.active.is_(True), Entity.entity_name.notin_(seen_names))
            ).scalars().all()
            for entity in stale:
                entity.active = False
                db.add(entity)
                deactivated += 1
        db.commit()
        return {
            "created": created,
            "updated": updated,
            "deactivated": deactivated,
            "duplicates": duplicates,
            "total": len(folders),
        }

    def update(self, db: Session, entity_id: int, aliases: list[str] | None = None, active: bool | None = None) -> Entity | None:
        # Reviewer-managed edits from the dashboard Entities page. Aliases are replaced
        # wholesale (deduped) so the UI can add and remove in one call.
        entity = db.get(Entity, entity_id)
        if not entity:
            return None
        if aliases is not None:
            entity.aliases = self._merge_aliases([], aliases)
        if active is not None:
            entity.active = active
        db.add(entity)
        db.commit()
        db.refresh(entity)
        return entity

    def add_alias(self, db: Session, entity_name: str, alias: str) -> bool:
        # Append a learned alias (e.g. "Willow Falls") to an entity so future matches hit
        # directly. The aliases list is reassigned so SQLAlchemy tracks the JSON change.
        alias = (alias or "").strip()
        if not alias:
            return False
        entity = self.get_by_name(db, entity_name)
        if not entity:
            return False
        existing = entity.aliases or []
        if any(alias.lower() == (item or "").strip().lower() for item in existing):
            return False
        entity.aliases = [*existing, alias]
        db.add(entity)
        return True

    def _aliases_from_folder_name(self, name: str) -> list[str]:
        aliases: list[str] = []
        if " - " in name:
            aliases.append(name.split(" - ", 1)[1].strip())
        aliases.append(name)
        more: list[str] = []
        for alias in aliases:
            if alias.lower().startswith("the "):
                more.append(alias[4:].strip())
            if alias.lower().endswith(" llc"):
                more.append(alias[:-4].strip())
            if alias.lower().startswith("the ") and alias.lower().endswith(" llc"):
                more.append(alias[4:-4].strip())
        return self._merge_aliases([], [*aliases, *more])

    def _merge_aliases(self, existing: list[str], additions: list[str]) -> list[str]:
        output: list[str] = []
        seen: set[str] = set()
        for alias in [*existing, *additions]:
            value = (alias or "").strip()
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            output.append(value)
        return output

    def best_matches_for_text(self, db: Session, text: str, limit: int = 10) -> list[dict]:
        haystack = text.lower()
        matches: list[dict] = []
        for entity in self.list_active(db):
            score = 0
            terms = [entity.entity_name, *(entity.aliases or []), *(entity.properties or [])]
            for term in terms:
                if term and term.lower() in haystack:
                    score += 1
            if score:
                matches.append({"entity_name": entity.entity_name, "score": score, "aliases": entity.aliases})
        return sorted(matches, key=lambda item: item["score"], reverse=True)[:limit]
