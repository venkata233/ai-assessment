from datetime import UTC, datetime, timedelta

import pytest

from fakes import NOW, Env
from shortener.domain.errors import LinkNotFound, ValidationFailed
from shortener.domain.models import ClickEvent
from shortener.services.analytics_service import decode_cursor, encode_cursor


def click(
    n: int,
    *,
    at: datetime,
    url_id: int = 1,
    referrer: str | None = None,
    family: str = "chrome",
    country: str | None = None,
    visitor: str | None = None,
) -> ClickEvent:
    return ClickEvent(
        f"{int(at.timestamp() * 1000)}-{n}", url_id, at, referrer, "UA", family, country, visitor
    )


async def seeded() -> Env:
    env = Env()
    await env.link_service.create(owner="demo", url="https://example.com/", alias="promo", expires_at=None)
    base = NOW - timedelta(days=1)
    events = [
        click(0, at=base, referrer="https://a.example", country="DE", visitor="aaaaaaaaaaaaaaaa"),
        click(
            1,
            at=base + timedelta(minutes=1),
            referrer="https://a.example",
            country="DE",
            visitor="aaaaaaaaaaaaaaaa",
        ),
        click(
            2,
            at=base + timedelta(hours=2),
            referrer="https://b.example",
            family="firefox",
            visitor="bbbbbbbbbbbbbbbb",
        ),
        click(3, at=base + timedelta(hours=3), referrer=None, family="curl"),
        click(4, at=base - timedelta(days=30)),  # outside the default 7-day window
        click(0, at=base, url_id=2, referrer="https://other.example"),  # belongs to some other link
    ]
    await env.clicks_repo.insert_batch(events)
    return env


async def report(env: Env, **kwargs: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "owner": "demo",
        "code": "promo",
        "start": None,
        "end": None,
        "granularity": "day",
        "limit": 20,
        "cursor": None,
    }
    params.update(kwargs)
    return await env.analytics.report(**params)  # type: ignore[arg-type]


async def test_report_aggregates_only_this_links_clicks_in_the_window() -> None:
    env = await seeded()
    result = await report(env)
    assert result.total_clicks == 4
    assert result.approx_unique_visitors == 2
    assert [(i.value, i.clicks) for i in result.top_referrers] == [
        ("https://a.example", 2),
        (None, 1),
        ("https://b.example", 1),
    ]
    assert next((i.value, i.clicks) for i in result.top_user_agents) == ("chrome", 2)
    assert [(i.value, i.clicks) for i in result.top_countries] == [(None, 2), ("DE", 2)]
    assert sum(b.clicks for b in result.clicks_over_time) == 4
    assert result.end == NOW and result.start == NOW - timedelta(days=7)


async def test_hourly_granularity_splits_buckets() -> None:
    env = await seeded()
    daily = await report(env, granularity="day")
    hourly = await report(env, granularity="hour")
    assert len(hourly.clicks_over_time) > len(daily.clicks_over_time)


async def test_explicit_window_is_half_open() -> None:
    env = await seeded()
    first = NOW - timedelta(days=1)
    result = await report(
        env, start=first, end=first + timedelta(minutes=1)
    )  # [from, to) excludes the 2nd click
    assert result.total_clicks == 1
    naive = await report(
        env, start=first.replace(tzinfo=None), end=(first + timedelta(minutes=1)).replace(tzinfo=None)
    )
    assert naive.total_clicks == 1  # naive datetimes are interpreted as UTC


async def test_recent_clicks_paginate_without_gaps_or_repeats() -> None:
    env = await seeded()
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await report(env, limit=2, cursor=cursor)
        seen.extend(c.event_id for c in page.recent_clicks)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 2
    assert len(seen) == len(set(seen)) == 4
    assert seen == sorted(
        seen,
        key=lambda e: next(c.clicked_at for c in env.clicks_repo.events if c.event_id == e),
        reverse=True,
    )


async def test_exact_page_boundary_has_no_next_cursor() -> None:
    env = await seeded()
    assert (await report(env, limit=4)).next_cursor is None
    assert (await report(env, limit=3)).next_cursor is not None


@pytest.mark.parametrize(
    ("kwargs", "field", "code"),
    [
        ({"start": NOW, "end": NOW}, "from", "window-invalid"),
        ({"start": NOW, "end": NOW - timedelta(hours=1)}, "from", "window-invalid"),
        ({"start": NOW - timedelta(days=91), "end": NOW}, "from", "window-too-large"),
        ({"cursor": "%%%not-base64%%%"}, "cursor", "cursor-invalid"),
        ({"cursor": encode_cursor(NOW, "1-1")[:-3]}, "cursor", "cursor-invalid"),
    ],
)
async def test_invalid_parameters(kwargs: dict[str, object], field: str, code: str) -> None:
    env = await seeded()
    with pytest.raises(ValidationFailed) as info:
        await report(env, **kwargs)
    assert (info.value.issues[0].field, info.value.issues[0].code) == (field, code)


async def test_ninety_day_window_is_allowed() -> None:
    env = await seeded()
    assert (await report(env, start=NOW - timedelta(days=90), end=NOW)).total_clicks == 5


async def test_analytics_are_owner_scoped() -> None:
    env = await seeded()
    with pytest.raises(LinkNotFound):
        await report(env, owner="other")
    with pytest.raises(LinkNotFound):
        await report(env, code="missing1")


def test_cursor_round_trip_and_validation() -> None:
    value = encode_cursor(NOW, "1700000000000-5")
    assert decode_cursor(value) == (NOW, "1700000000000-5")
    assert "=" not in value
    import base64

    def forge(raw: str) -> str:
        return base64.urlsafe_b64encode(raw.encode()).decode()

    for bad in (
        forge("2026-11-20T12:00:00|x'; DROP TABLE clicks;--"),
        forge("2026-11-20T12:00:00|1-1"),
        forge("nonsense"),
    ):
        with pytest.raises(ValidationFailed):
            decode_cursor(bad)
    assert decode_cursor(encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), "1-1"))[0].tzinfo is not None
