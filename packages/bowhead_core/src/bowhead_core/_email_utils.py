"""Email presentation and canonicalization helpers."""

from __future__ import annotations


def normalize_email(email: str) -> str:
    """Return Bowhead's single persisted email representation.

    Identity matching deliberately uses a small, deterministic rule: trim
    surrounding whitespace then lowercase.  It is used at every read and
    write boundary; validation remains the caller's responsibility.
    """
    return email.strip().lower()


def mask_email(email: str | None) -> str:
    """Mask email for audit log / UI to avoid PII leak.

    Examples:
        alice@datarc.cn  → a***@datarc.cn
        ab@x.com         → a***@x.com
        a@x.com          → ***@x.com (1-char local → fully mask)
        None / ''        → ***
    """
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.rpartition("@")
    if len(local) <= 1:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"
