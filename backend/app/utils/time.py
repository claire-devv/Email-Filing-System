from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    # SQLite does not persist timezone info, so DateTime(timezone=True) columns read back
    # naive. Treat a naive value as UTC so it can be safely compared/subtracted with the
    # aware utc_now(). Aware values pass through unchanged.
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def date_prefix(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.strftime("%Y.%m.%d")
