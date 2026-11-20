import re
import time

import pytest

from conftest import FAKE_AWS, FAKE_GH, FAKE_PASSWORD, FAKE_SK
from copilot.redaction import Redactor, scan

JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    + "."
    + "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
    + "."
    + "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("kind", "text", "secret"),
    [
        ("AWS_ACCESS_KEY", f"key id {FAKE_AWS} in config", FAKE_AWS),
        ("GITHUB_TOKEN", f"token {FAKE_GH} here", FAKE_GH),
        ("API_KEY", f"OPENAI={FAKE_SK}", FAKE_SK),
        ("JWT", f"cookie: {JWT}", JWT),
        (
            "PRIVATE_KEY",
            f"before\n{PRIVATE_KEY}\nafter",
            "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu",
        ),
        ("BEARER_TOKEN", "Authorization: Bearer abcdefghijklmnop1234567890", "abcdefghijklmnop1234567890"),
        ("URL_CREDENTIALS", "postgres://appuser:s3cretPass@db.internal:5432/app", "s3cretPass"),
        ("SECRET_VALUE", f'password = "{FAKE_PASSWORD}"', FAKE_PASSWORD),
        ("SECRET_VALUE", f"api_key: {FAKE_PASSWORD}", FAKE_PASSWORD),
        ("EMAIL", "ask jane.doe+test@example.co.uk about it", "jane.doe+test@example.co.uk"),
        ("IPV4", "server at 203.0.113.42 responded", "203.0.113.42"),
        ("PHONE", "call +44 20 7946 0958 today", "+44 20 7946 0958"),
        ("CREDIT_CARD", "card 4111 1111 1111 1111 expires", "4111 1111 1111 1111"),
    ],
)
def test_each_category_is_redacted_and_counted(kind: str, text: str, secret: str) -> None:
    redacted, report = Redactor().redact(text)
    assert secret not in redacted
    assert f"[REDACTED:{kind}:1]" in redacted
    assert report.counts.get(kind) == 1


def test_surrounding_text_is_preserved() -> None:
    redacted, _ = Redactor().redact(
        'db: postgres://appuser:s3cretPass@db.internal:5432/app and password="correct-horse-1"'
    )
    assert "postgres://[REDACTED:URL_CREDENTIALS:1]@db.internal:5432/app" in redacted
    assert 'password="[REDACTED:SECRET_VALUE:1]"' in redacted


def test_the_same_value_gets_the_same_placeholder_and_new_values_new_ones() -> None:
    redactor = Redactor()
    first, _ = redactor.redact("a@example.com wrote to b@example.com and again to a@example.com")
    assert first.count("[REDACTED:EMAIL:1]") == 2
    assert "[REDACTED:EMAIL:2]" in first
    later, _ = redactor.redact("a@example.com")  # numbering is stable across calls within a session
    assert later == "[REDACTED:EMAIL:1]"


def test_report_holds_counts_only() -> None:
    _, report = Redactor().redact(f"{FAKE_AWS} {FAKE_GH} {FAKE_PASSWORD}")
    assert all(isinstance(v, int) for v in report.counts.values())
    assert FAKE_AWS not in str(report.counts) and FAKE_GH not in str(report.counts)


def test_redaction_is_idempotent() -> None:
    once, _ = Redactor().redact(f"{FAKE_AWS} password={FAKE_PASSWORD} mail a@example.com 203.0.113.9")
    twice, report = Redactor().redact(once)
    assert twice == once and report.total == 0


def test_ordinary_text_is_left_alone() -> None:
    prose = (
        "Build a scalable service. Use version 1.2.3 and port 8080; the token bucket limits to 30 per minute."
    )
    assert Redactor().redact(prose)[0] == prose


def test_loopback_and_unspecified_addresses_are_kept_but_public_ones_are_not() -> None:
    redacted, _ = Redactor().redact("bind 127.0.0.1 and 0.0.0.0, peer 8.8.8.8")
    assert "127.0.0.1" in redacted and "0.0.0.0" in redacted and "8.8.8.8" not in redacted


def test_random_digit_runs_are_not_mistaken_for_cards() -> None:
    redacted, report = Redactor().redact("order 1234 5678 9012 3456 shipped")  # fails the Luhn check
    assert "1234 5678 9012 3456" in redacted and report.total == 0


def test_unterminated_private_key_is_still_removed() -> None:
    redacted, _ = Redactor().redact("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC")
    assert "MIIEvQIBADANBg" not in redacted


def test_scan_reports_without_changing() -> None:
    assert scan(f"x {FAKE_AWS}").counts == {"AWS_ACCESS_KEY": 1}
    assert scan("nothing here").total == 0


def test_secret_variable_names_with_the_keyword_inside_them_are_redacted() -> None:
    cases = {
        'ADMIN_PASSWORD = "hunter2-example-only"': "hunter2-example-only",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "GITHUB_TOKEN: abcd1234efgh": "abcd1234efgh",
        "db.password: s3cretvalue": "s3cretvalue",
        "STRIPE-API-KEY = zzzz9999yyyy": "zzzz9999yyyy",
    }
    for text, secret in cases.items():
        redacted, report = Redactor().redact(text)
        assert secret not in redacted and report.counts == {"SECRET_VALUE": 1}, text
        assert (
            redacted.split("=")[0].split(":")[0].strip() == text.split("=")[0].split(":")[0].strip()
        )  # the name stays readable


def test_short_values_and_unrelated_names_are_left_alone() -> None:
    for text in (
        "max_tokens = 30",
        "token bucket limits to 30 per minute",
        "passport = 'x'",
        "secretary: Jo",
    ):
        assert Redactor().redact(text)[1].total == 0, text


def test_name_scanning_stays_linear_on_pathological_input() -> None:
    nasty = "a_" * 25_000 + "\n" + "token" * 10_000 + "\n" + "x.y-" * 20_000
    started = time.perf_counter()
    Redactor().redact(nasty)
    assert time.perf_counter() - started < 3


def test_large_adversarial_input_is_processed_quickly() -> None:
    nasty = ("-----BEGIN PRIVATE KEY-----" + "A" * 50) * 200 + "a" * 50_000 + ("1 " * 5000) + ("x@" * 5000)
    started = time.perf_counter()
    Redactor().redact(nasty)
    assert time.perf_counter() - started < 3  # no catastrophic backtracking


def test_placeholder_format_is_machine_readable() -> None:
    redacted, _ = Redactor().redact(FAKE_AWS)
    assert re.fullmatch(r"\[REDACTED:[A-Z_]+:\d+\]", redacted)
