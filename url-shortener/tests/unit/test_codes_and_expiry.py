import string
from datetime import UTC, datetime, timedelta, timezone

import pytest

from shortener.domain import codes
from shortener.domain.codes import BASE62, RandomBase62Generator, is_reserved, validate_alias
from shortener.domain.errors import ValidationFailed
from shortener.domain.expiry import MAX_EXPIRY_DAYS, validate_expiry

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("alias", ["abc", "Promo", "promo", "a-b_c", "X" * 32, "2026-sale"])
def test_valid_aliases(alias: str) -> None:
    assert validate_alias(alias) == alias


@pytest.mark.parametrize(
    ("alias", "code"),
    [
        ("ab", "alias-invalid"),
        ("x" * 33, "alias-invalid"),
        ("has space", "alias-invalid"),
        ("dot.dot", "alias-invalid"),
        ("slash/x", "alias-invalid"),
        ("ünïcode", "alias-invalid"),
        ("", "alias-invalid"),
        ("api", "alias-reserved"),
        ("API", "alias-reserved"),
        ("Health", "alias-reserved"),
        ("docs", "alias-reserved"),
        ("metrics", "alias-reserved"),
    ],
)
def test_invalid_and_reserved_aliases(alias: str, code: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        validate_alias(alias)
    assert info.value.issues[0].field == "alias"
    assert info.value.issues[0].code == code


def test_generator_output_shape() -> None:
    generator = RandomBase62Generator(7)
    samples = {generator.generate() for _ in range(500)}
    assert len(samples) == 500  # 62^7 space: a collision here would indicate a broken source of randomness
    assert all(len(s) == 7 and set(s) <= set(string.ascii_letters + string.digits) for s in samples)


def test_alphabet_is_exactly_62_distinct_symbols() -> None:
    assert len(BASE62) == 62 == len(set(BASE62))


def test_generator_uses_the_secrets_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_choice(seq: str) -> str:
        calls.append(len(seq))
        return seq[0]

    monkeypatch.setattr(codes.secrets, "choice", fake_choice)
    assert RandomBase62Generator(7).generate() == "0" * 7
    assert calls == [62] * 7


def test_generator_never_returns_a_reserved_word(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(list("metrics") + list("Zx81QpA"))  # "metrics" is 7 letters and reserved
    monkeypatch.setattr(codes.secrets, "choice", lambda _seq: next(answers))
    assert RandomBase62Generator(7).generate() == "Zx81QpA"
    assert is_reserved("metrics")


def test_generator_rejects_unsafe_lengths() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        RandomBase62Generator(5)


def test_no_expiry_is_fine() -> None:
    assert validate_expiry(None, NOW) is None


def test_future_expiry_is_normalised_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    value = validate_expiry(datetime(2027, 1, 1, 12, 0, tzinfo=plus_two), NOW)
    assert value == datetime(2027, 1, 1, 10, 0, tzinfo=UTC)
    assert value is not None and value.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (datetime(2026, 11, 20, 11, 59, 59, tzinfo=UTC), "expiry-in-past"),
        (NOW, "expiry-in-past"),  # "now" is already expired: the boundary is exclusive
        (datetime(2027, 1, 1), "expiry-timezone-required"),  # naive
        (NOW + timedelta(days=MAX_EXPIRY_DAYS, seconds=1), "expiry-too-far"),
    ],
)
def test_expiry_rejections(value: datetime, code: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        validate_expiry(value, NOW)
    assert info.value.issues[0].code == code


def test_expiry_accepts_the_exact_maximum() -> None:
    assert validate_expiry(NOW + timedelta(days=MAX_EXPIRY_DAYS), NOW) is not None
