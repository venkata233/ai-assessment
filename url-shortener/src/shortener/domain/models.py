"""Core entities. Pure data, no I/O."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LinkStatus = Literal["active", "expired"]


@dataclass(frozen=True)
class Link:
    id: int
    code: str
    target_url: str
    owner_key_id: str
    is_custom_alias: bool
    click_count: int
    created_at: datetime
    expires_at: datetime | None
    deleted_at: datetime | None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now

    def status(self, now: datetime) -> LinkStatus:
        return "expired" if self.is_expired(now) else "active"


@dataclass(frozen=True)
class ClickEvent:
    """A click as stored in PostgreSQL. `event_id` is the Redis Stream entry id."""

    event_id: str
    url_id: int
    clicked_at: datetime
    referrer: str | None
    user_agent: str | None
    ua_family: str
    country: str | None
    visitor_hash: str | None


@dataclass(frozen=True)
class ClickRecord:
    """Producer-side click, before it has a stream id."""

    url_id: int
    ts_ms: int
    referrer: str | None
    user_agent: str | None
    ua_family: str
    country: str | None
    visitor_hash: str | None
