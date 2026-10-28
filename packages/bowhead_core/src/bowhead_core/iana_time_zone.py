"""Safe lookup of user-supplied IANA time zone names.

``ZoneInfo(name)`` alone is not a validator: directory names under the tz
database ("America", "Etc") raise ``IsADirectoryError`` and files such as
``posixrules`` load as zones. Callers that take a zone from a request, a stored
job or a client header resolve it here, which accepts only names listed by
``zoneinfo.available_timezones()``.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

_MAX_NAME_LEN = 64
# Listed by some tz databases but not real zones a user would pick.
_EXCLUDED = frozenset({"posixrules", "localtime", "Factory"})


@lru_cache(maxsize=1)
def _known_zones() -> frozenset[str]:
    return frozenset(available_timezones()) - _EXCLUDED


def load_iana_time_zone(name: object) -> ZoneInfo | None:
    """The zone for a known IANA ``name``, or None for anything else (never raises)."""
    if not isinstance(name, str):
        return None
    key = name.strip()
    if not key or len(key) > _MAX_NAME_LEN or key not in _known_zones():
        return None
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
