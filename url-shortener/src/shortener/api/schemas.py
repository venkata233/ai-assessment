"""Request/response models. They mirror components.schemas in openapi.yaml (contract tests enforce it)."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shortener.domain.errors import ValidationFailed
from shortener.domain.models import ClickEvent, Link
from shortener.services.analytics_service import AnalyticsReport


class CreateUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Generous hard caps only protect against absurd payloads; the precise rules live in the domain layer.
    url: str = Field(max_length=8192)
    alias: str | None = Field(default=None, max_length=64)
    expires_at: str | None = Field(default=None, max_length=64)

    def parsed_expires_at(self) -> datetime | None:
        if self.expires_at is None:
            return None
        try:
            return datetime.fromisoformat(self.expires_at)
        except ValueError:
            raise ValidationFailed.single(
                "expires_at", "expiry-invalid", "expires_at must be an ISO-8601 date-time with a timezone."
            ) from None


class UrlResource(BaseModel):
    code: str
    short_url: str
    target_url: str
    is_custom_alias: bool
    status: Literal["active", "expired"]
    click_count: int
    created_at: datetime
    expires_at: datetime | None

    @classmethod
    def from_link(cls, link: Link, *, base_url: str, now: datetime) -> "UrlResource":
        return cls(
            code=link.code,
            short_url=f"{base_url}/{link.code}",
            target_url=link.target_url,
            is_custom_alias=link.is_custom_alias,
            status=link.status(now),
            click_count=link.click_count,
            created_at=link.created_at,
            expires_at=link.expires_at,
        )


class TimeBucketModel(BaseModel):
    bucket_start: datetime
    clicks: int


class CountItemModel(BaseModel):
    value: str | None
    clicks: int


class ClickModel(BaseModel):
    clicked_at: datetime
    referrer: str | None
    user_agent: str | None
    user_agent_family: str
    country: str | None

    @classmethod
    def from_event(cls, event: ClickEvent) -> "ClickModel":
        return cls(
            clicked_at=event.clicked_at,
            referrer=event.referrer,
            user_agent=event.user_agent,
            user_agent_family=event.ua_family,
            country=event.country.strip() if event.country else None,
        )


class ClickPageModel(BaseModel):
    items: list[ClickModel]
    next_cursor: str | None


class AnalyticsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    from_: datetime = Field(alias="from")
    to: datetime
    granularity: Literal["hour", "day"]
    total_clicks: int
    approx_unique_visitors: int
    clicks_over_time: list[TimeBucketModel]
    top_referrers: list[CountItemModel]
    top_user_agents: list[CountItemModel]
    top_countries: list[CountItemModel]
    recent_clicks: ClickPageModel

    @classmethod
    def from_report(cls, report: AnalyticsReport) -> "AnalyticsResponse":
        return cls(
            code=report.code,
            **{"from": report.start.astimezone(UTC)},
            to=report.end.astimezone(UTC),
            granularity=report.granularity,
            total_clicks=report.total_clicks,
            approx_unique_visitors=report.approx_unique_visitors,
            clicks_over_time=[
                TimeBucketModel(bucket_start=b.bucket_start, clicks=b.clicks) for b in report.clicks_over_time
            ],
            top_referrers=[CountItemModel(value=i.value, clicks=i.clicks) for i in report.top_referrers],
            top_user_agents=[CountItemModel(value=i.value, clicks=i.clicks) for i in report.top_user_agents],
            top_countries=[CountItemModel(value=i.value, clicks=i.clicks) for i in report.top_countries],
            recent_clicks=ClickPageModel(
                items=[ClickModel.from_event(e) for e in report.recent_clicks], next_cursor=report.next_cursor
            ),
        )


class Readiness(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    checks: dict[str, Literal["ok", "down"]]
