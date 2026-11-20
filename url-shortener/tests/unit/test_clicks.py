import re
from datetime import date

import pytest

from shortener.domain.clicks import (
    country_from_header,
    referrer_origin,
    truncate_user_agent,
    ua_family,
    visitor_hash,
)

DB_ORIGIN_CHECK = re.compile(r"^https?://[^/?#@[:space:]]+$".replace("[:space:]", r"\s"))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("https://news.example.com/article/42?token=SECRET#frag", "https://news.example.com"),
        ("HTTP://Example.COM:8080/a/b", "http://example.com:8080"),
        ("https://user:pw@example.com/private", "https://example.com"),  # userinfo dropped
        ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443"),
        ("https://example.com", "https://example.com"),
        (None, None),
        ("", None),
        ("   ", None),
        ("ftp://example.com/x", None),
        ("javascript:alert(1)", None),
        ("/relative/path", None),
        ("not a url", None),
        ("https://exa mple.com/", None),
        ("https://example.com:notaport/", None),
        ("https://" + "a" * 300 + ".com/", None),  # longer than the 255-character column
        ("https:///path-only", None),
    ],
)
def test_referrer_is_reduced_to_an_origin(header: str | None, expected: str | None) -> None:
    result = referrer_origin(header)
    assert result == expected
    if result is not None:
        assert DB_ORIGIN_CHECK.match(result), "must satisfy the database CHECK constraint"
        assert "SECRET" not in result


@pytest.mark.parametrize(
    ("agent", "family"),
    [
        ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "chrome"),
        ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120.0 Safari/537.36 Edg/120.0", "edge"),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0", "firefox"),
        ("Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15", "safari"),
        ("curl/8.4.0", "curl"),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "bot"),
        ("facebookexternalhit/1.1", "bot"),
        ("SomethingElse/1.0", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_user_agent_family(agent: str | None, family: str) -> None:
    assert ua_family(agent) == family


def test_user_agent_is_truncated_and_stripped_of_control_characters() -> None:
    assert truncate_user_agent("A" * 400) == "A" * 255
    assert truncate_user_agent("Mozilla\x00\x1b[31m/5.0") == "Mozilla[31m/5.0"
    assert truncate_user_agent("") is None
    assert truncate_user_agent(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("de", "DE"),
        (" us ", "US"),
        ("XX", None),
        ("T1", None),
        ("USA", None),
        ("", None),
        (None, None),
        ("1A", None),
    ],
)
def test_country_header(value: str | None, expected: str | None) -> None:
    assert country_from_header(value) == expected


def test_visitor_hash_properties() -> None:
    day = date(2026, 11, 20)
    base = visitor_hash("secret-secret-secret", "203.0.113.9", "UA/1", day)
    assert base is not None and re.fullmatch(r"[0-9a-f]{16}", base)
    assert base == visitor_hash("secret-secret-secret", "203.0.113.9", "UA/1", day)  # stable within a day
    assert base != visitor_hash(
        "secret-secret-secret", "203.0.113.9", "UA/1", date(2026, 11, 21)
    )  # rotates daily
    assert base != visitor_hash("secret-secret-secret", "203.0.113.10", "UA/1", day)
    assert base != visitor_hash("secret-secret-secret", "203.0.113.9", "UA/2", day)
    assert base != visitor_hash("another-secret-value", "203.0.113.9", "UA/1", day)
    assert "203.0.113.9" not in base
    assert visitor_hash("secret-secret-secret", None, "UA/1", day) is None
