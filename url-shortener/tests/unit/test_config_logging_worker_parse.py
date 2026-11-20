import json
import logging

import pytest
import structlog
from pydantic import ValidationError

from shortener.config import Settings, parse_api_keys
from shortener.logging_config import REDACTED, configure_logging, redact_sensitive
from shortener.worker.consumer import PoisonMessage, parse_message

HASH = "a" * 64
REQUIRED = {
    "database_url": "postgresql+asyncpg://u:p@h/db",
    "redis_url": "redis://h:6379/0",
    "api_keys": f"demo:{HASH}",
    "base_url": "http://localhost:8000",
    "analytics_secret": "x" * 16,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "REDIS_URL", "API_KEYS", "BASE_URL", "ANALYTICS_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_startup_fails_fast_when_a_required_setting_is_missing(missing: str) -> None:
    values = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv("CODE_LENGTH", "8")
    settings = Settings()  # type: ignore[call-arg]
    assert settings.code_length == 8
    assert settings.api_key_hashes == {HASH: "demo"}
    assert settings.base_url == "http://localhost:8000"
    assert "p@h" not in repr(settings) and "xxxxxxxx" not in repr(settings)  # secrets are masked


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql://u:p@h/db"),  # wrong driver
        ("base_url", "ftp://localhost"),
        ("base_url", "http://localhost/path"),
        ("analytics_secret", "short"),
        ("api_keys", "demo:not-hex"),
        ("api_keys", ""),
        ("code_length", 3),
        ("trusted_proxy_hops", -1),
    ],
)
def test_invalid_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**REQUIRED, field: value})  # type: ignore[arg-type]


def test_base_url_trailing_slash_is_normalised() -> None:
    assert Settings(**{**REQUIRED, "base_url": "https://s.example/"}).base_url == "https://s.example"  # type: ignore[arg-type]


def test_api_key_parsing() -> None:
    other = "b" * 64
    assert parse_api_keys(f" demo:{HASH} , ops-1:{other.upper()} ") == {HASH: "demo", other: "ops-1"}
    for bad in (
        f"demo:{HASH},demo:{other}",
        f"a:{HASH},b:{HASH}",
        "nocolon",
        f"bad label:{HASH}",
        "a:" + "g" * 64,
        ",",
    ):
        with pytest.raises(ValueError):  # noqa: PT011
            parse_api_keys(bad)


def test_redaction_processor_masks_sensitive_keys() -> None:
    event = {
        "event": "x",
        "api_key": "k",
        "X-API-Key": "k",
        "ip": "1.2.3.4",
        "user_agent": "UA",
        "route": "/x",
    }
    result = redact_sensitive(None, "info", dict(event))
    assert {k: v for k, v in result.items() if v == REDACTED} == {
        "api_key": REDACTED,
        "X-API-Key": REDACTED,
        "ip": REDACTED,
        "user_agent": REDACTED,
    }
    assert result["route"] == "/x"


def test_configured_logger_emits_json_without_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        configure_logging("INFO")
        structlog.get_logger().info(
            "request", route="/{code}", api_key="SUPER-SECRET", client_ip="203.0.113.9"
        )
        line = capsys.readouterr().out.strip().splitlines()[-1]
    finally:
        structlog.reset_defaults()  # do not leak a logger bound to the captured stream into other tests
        logging.getLogger().handlers.clear()
    payload = json.loads(line)
    assert payload["event"] == "request" and payload["level"] == "info" and "timestamp" in payload
    assert "SUPER-SECRET" not in line and "203.0.113.9" not in line


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"url_id": "x", "ts_ms": "1700000000000"},
        {"url_id": "1", "ts_ms": "nan"},
        {"url_id": "0", "ts_ms": "1700000000000"},
        {"url_id": "1", "ts_ms": "0"},
        {"url_id": "1", "ts_ms": "99999999999999"},
    ],
)
def test_malformed_stream_entries_are_poison(fields: dict[str, str]) -> None:
    with pytest.raises(PoisonMessage):
        parse_message("1700000000000-0", fields)


def test_stream_entry_fields_are_re_sanitised() -> None:
    event = parse_message(
        "1700000000000-3",
        {
            "url_id": "7",
            "ts_ms": "1700000000000",
            "referrer_origin": "https://evil.example/path?token=1",
            "ua": "UA" * 300,
            "ua_family": "f" * 99,
            "country": "zz9",
            "visitor_hash": "NOT-HEX",
        },
    )
    assert event.event_id == "1700000000000-3" and event.url_id == 7
    assert event.referrer == "https://evil.example"  # a hostile producer cannot smuggle a path into the DB
    assert event.user_agent is not None and len(event.user_agent) == 255
    assert len(event.ua_family) == 32
    assert event.country is None and event.visitor_hash is None
    assert event.clicked_at.isoformat() == "2023-11-14T22:13:20+00:00"


def test_empty_optional_fields_become_null() -> None:
    event = parse_message(
        "1700000000000-0",
        {
            "url_id": "1",
            "ts_ms": "1700000000000",
            "referrer_origin": "",
            "ua": "",
            "ua_family": "",
            "country": "",
            "visitor_hash": "",
        },
    )
    assert (event.referrer, event.user_agent, event.country, event.visitor_hash) == (None, None, None, None)
    assert event.ua_family == "other"
