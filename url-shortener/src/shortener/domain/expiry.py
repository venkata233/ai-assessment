"""Expiry rules."""

from datetime import UTC, datetime, timedelta

from shortener.domain.errors import ValidationFailed

MAX_EXPIRY_DAYS = 1826  # five years, including one leap day


def validate_expiry(expires_at: datetime | None, now: datetime) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValidationFailed.single(
            "expires_at", "expiry-timezone-required", "expires_at must include a timezone."
        )
    value = expires_at.astimezone(UTC)
    if value <= now:
        raise ValidationFailed.single("expires_at", "expiry-in-past", "expires_at must be in the future.")
    if value > now + timedelta(days=MAX_EXPIRY_DAYS):
        raise ValidationFailed.single(
            "expires_at", "expiry-too-far", "expires_at must be at most 5 years in the future."
        )
    return value
