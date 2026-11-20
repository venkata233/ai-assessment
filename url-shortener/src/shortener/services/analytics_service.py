"""Read-only, owner-scoped analytics over the click store."""

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from shortener.domain.errors import FieldIssue, ValidationFailed
from shortener.domain.models import ClickEvent
from shortener.ports import ClickRepository, Clock, CountItem, TimeBucket
from shortener.services.link_service import LinkService

DEFAULT_WINDOW = timedelta(days=7)
MAX_WINDOW = timedelta(days=90)
TOP_N = 10
_EVENT_ID_RE = re.compile(r"^[0-9]{1,20}-[0-9]{1,20}$")

Granularity = Literal["hour", "day"]


@dataclass(frozen=True)
class AnalyticsReport:
    code: str
    start: datetime
    end: datetime
    granularity: Granularity
    total_clicks: int
    approx_unique_visitors: int
    clicks_over_time: list[TimeBucket]
    top_referrers: list[CountItem]
    top_user_agents: list[CountItem]
    top_countries: list[CountItem]
    recent_clicks: list[ClickEvent]
    next_cursor: str | None


def encode_cursor(clicked_at: datetime, event_id: str) -> str:
    raw = f"{clicked_at.astimezone(UTC).isoformat()}|{event_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        timestamp, _, event_id = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        parsed = datetime.fromisoformat(timestamp)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValidationFailed.single("cursor", "cursor-invalid", "The cursor is not valid.") from None
    if parsed.tzinfo is None or not _EVENT_ID_RE.match(event_id):
        raise ValidationFailed.single("cursor", "cursor-invalid", "The cursor is not valid.")
    return parsed, event_id


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AnalyticsService:
    def __init__(self, *, links: LinkService, clicks: ClickRepository, clock: Clock) -> None:
        self._links = links
        self._clicks = clicks
        self._clock = clock

    async def report(
        self,
        *,
        owner: str,
        code: str,
        start: datetime | None,
        end: datetime | None,
        granularity: Granularity,
        limit: int,
        cursor: str | None,
    ) -> AnalyticsReport:
        link = await self._links.get(owner=owner, code=code)  # 404 for non-owners, before anything else
        window_end = _as_utc(end) if end is not None else self._clock.now()
        window_start = _as_utc(start) if start is not None else window_end - DEFAULT_WINDOW
        issues: list[FieldIssue] = []
        if window_start >= window_end:
            issues.append(FieldIssue("from", "window-invalid", "`from` must be before `to`."))
        elif window_end - window_start > MAX_WINDOW:
            issues.append(FieldIssue("from", "window-too-large", "The window may not exceed 90 days."))
        decoded = None
        if cursor is not None:
            try:
                decoded = decode_cursor(cursor)
            except ValidationFailed as exc:
                issues.extend(exc.issues)
        if issues:
            raise ValidationFailed(issues)

        totals, series, referrers, agents, countries, page = await asyncio.gather(
            self._clicks.totals(link.id, window_start, window_end),
            self._clicks.time_series(link.id, window_start, window_end, granularity),
            self._clicks.top_values(link.id, "referrer", window_start, window_end, TOP_N),
            self._clicks.top_values(link.id, "ua_family", window_start, window_end, TOP_N),
            self._clicks.top_values(link.id, "country", window_start, window_end, TOP_N),
            self._clicks.recent(link.id, window_start, window_end, decoded, limit + 1),
        )
        has_more = len(page) > limit
        items = page[:limit]
        next_cursor = encode_cursor(items[-1].clicked_at, items[-1].event_id) if has_more else None
        return AnalyticsReport(
            code=link.code,
            start=window_start,
            end=window_end,
            granularity=granularity,
            total_clicks=totals.total,
            approx_unique_visitors=totals.unique_visitors,
            clicks_over_time=series,
            top_referrers=referrers,
            top_user_agents=agents,
            top_countries=countries,
            recent_clicks=items,
            next_cursor=next_cursor,
        )
