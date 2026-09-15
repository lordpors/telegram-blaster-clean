from datetime import timezone
from zoneinfo import ZoneInfo


JAKARTA = ZoneInfo("Asia/Jakarta")


def jakarta_time(value, fmt="%d %b %Y %H:%M"):
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JAKARTA).strftime(fmt)
