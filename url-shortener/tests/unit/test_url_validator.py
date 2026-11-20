import ipaddress

import pytest

from shortener.domain.errors import ValidationFailed
from shortener.domain.url_validator import (
    MAX_URL_LENGTH,
    is_public_ip,
    parse_legacy_ipv4,
    validate_target_url,
)

OWN = frozenset({("short.test", 80)})


def code_of(raw: str) -> str:
    with pytest.raises(ValidationFailed) as info:
        validate_target_url(raw, own_origins=OWN)
    assert [i.field for i in info.value.issues] == ["url"]
    return info.value.issues[0].code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
        ("HTTP://EXAMPLE.com", "http://example.com/"),
        ("https://example.com:443/x", "https://example.com/x"),
        ("http://example.com:8080/", "http://example.com:8080/"),
        ("https://example.com/a%20b?q=%C3%BC", "https://example.com/a%20b?q=%C3%BC"),
        ("https://example.com/ü?q=é", "https://example.com/%C3%BC?q=%C3%A9"),
        ("https://münchen.de/", "https://xn--mnchen-3ya.de/"),
        ("http://8.8.8.8/", "http://8.8.8.8/"),
        ("http://134744072/", "http://8.8.8.8/"),  # decimal form of a *public* address is normalised
        ("http://[2606:4700:4700::1111]/x", "http://[2606:4700:4700::1111]/x"),
        ("https://short.test/x", "https://short.test/x"),  # same host, different port than our own origin
        ("https://sub.example.co.uk./", "https://sub.example.co.uk/"),
    ],
)
def test_accepts_and_normalises(raw: str, expected: str) -> None:
    assert validate_target_url(raw, own_origins=OWN).url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/",
        "example.com/path",
        "//example.com/path",
        "mailto:a@b.co",
    ],
)
def test_rejects_non_http_schemes(raw: str) -> None:
    assert code_of(raw) == "scheme-not-allowed"


@pytest.mark.parametrize(
    "raw",
    ["https://user:pass@example.com/", "https://user@example.com/", "https://example.com@evil.com/"],
)
def test_rejects_credentials(raw: str) -> None:
    assert code_of(raw) == "credentials-not-allowed"


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1/",
        "http://127.1/",  # short form
        "http://0x7f.1/",  # hex component
        "http://0x7f000001/",  # whole address in hex
        "http://0177.0.0.1/",  # octal
        "http://2130706433/",  # decimal integer
        "http://0.0.0.0/",
        "http://10.0.0.1/",
        "http://172.16.5.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.100.100.200/",  # shared address space (Alibaba metadata)
        "http://224.0.0.1/",  # multicast
        "http://255.255.255.255/",
        "http://127.0.0.1:8080/",
        "http://[::1]/",
        "http://[::]/",
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped
        "http://[::ffff:7f00:1]/",  # IPv4-mapped, hex notation
        "http://[::127.0.0.1]/",  # IPv4-compatible
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[64:ff9b::7f00:1]/",  # NAT64 embedding 127.0.0.1
        "http://[2002:7f00:1::]/",  # 6to4 embedding 127.0.0.1
        "http://127。0。0。1/",  # ideographic full stops fold to dots
        "http://①②⑦.0.0.1/",  # circled digits fold to digits
    ],
)
def test_rejects_non_public_addresses_in_every_encoding(raw: str) -> None:
    assert code_of(raw) == "private-address"


@pytest.mark.parametrize(
    "raw",
    [
        "http://localhost/",
        "http://LOCALHOST/",
        "http://localhost./",
        "http://api.localhost/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "http://app.internal/",
        "http://router.lan/",
        "http://intranet/",  # single label: resolves through search domains
    ],
)
def test_rejects_internal_host_names(raw: str) -> None:
    assert code_of(raw) == "host-not-allowed"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "http://",
        "http://:80/",
        "http://example.com:99999/",
        "http://example.com:0/",
        "http://[::1/",
        "http://[fe80::1%25eth0]/",  # zone ids are never legitimate here
        "http://%31%32%37.0.0.1/",  # percent-encoded host
        "http://1.2.3.4.5/",
        "http://256.1.1.1/",
        "http://example.123/",  # numeric TLD is parsed as IPv4 by browsers, and is invalid
        "http://-bad.example.com/",
        "http://a..b.com/",
        "http://exa_mple$.com/",
    ],
)
def test_rejects_malformed_urls(raw: str) -> None:
    assert code_of(raw) == "invalid-url"


@pytest.mark.parametrize(
    "raw",
    [
        "https://exa\x00mple.com/",
        "https://example.com/a b",
        "https://example.com/\n",
        "https://example.com/\t",
        "https://example.com/\u202e",  # right-to-left override (format character)
        "https://example.com/\u00a0",  # no-break space
        "https://evil.com\\@127.0.0.1/",  # backslash confuses parsers
    ],
)
def test_rejects_control_whitespace_and_backslash(raw: str) -> None:
    assert code_of(raw) == "control-characters"


def test_rejects_overlong_urls() -> None:
    assert code_of("https://example.com/" + "a" * MAX_URL_LENGTH) == "url-too-long"


def test_accepts_exactly_max_length() -> None:
    raw = "https://example.com/" + "a" * (MAX_URL_LENGTH - len("https://example.com/"))
    assert validate_target_url(raw).url == raw


@pytest.mark.parametrize("raw", ["http://short.test/abc", "http://SHORT.test:80/x", "http://short.test./"])
def test_rejects_links_back_to_this_service(raw: str) -> None:
    assert code_of(raw) == "self-reference"


def test_own_origin_check_is_skipped_when_not_configured() -> None:
    assert validate_target_url("http://short.test/abc").host == "short.test"


@pytest.mark.parametrize(
    "private",
    ["127.0.0.1", "10.1.2.3", "172.31.255.254", "192.168.0.7", "169.254.1.1", "100.64.0.1", "0.1.2.3"],
)
def test_every_encoding_of_a_private_address_is_rejected(private: str) -> None:
    value = int(ipaddress.IPv4Address(private))
    octets = [int(p) for p in private.split(".")]
    forms = {
        "dotted": private,
        "decimal": str(value),
        "hex": hex(value),
        "octal-dotted": ".".join("0" + format(o, "o") for o in octets),
        "hex-dotted": ".".join(hex(o) for o in octets),
        "mapped-ipv6": f"[::ffff:{private}]",
    }
    for name, host in forms.items():
        assert code_of(f"http://{host}/") == "private-address", name


def test_legacy_ipv4_parser_round_trips_public_forms() -> None:
    assert parse_legacy_ipv4("8.8.8.8") == ipaddress.IPv4Address("8.8.8.8")
    assert parse_legacy_ipv4("8.8.2056") == ipaddress.IPv4Address("8.8.8.8")
    assert parse_legacy_ipv4("0x08080808") == ipaddress.IPv4Address("8.8.8.8")
    assert parse_legacy_ipv4("example.com") is None
    assert parse_legacy_ipv4("1.2.3.4.example.com") is None  # last label is not numeric: it is a DNS name


@pytest.mark.parametrize("bad", ["1.2.3.4.5", "1.2.3.999", "09.0.0.1", "4294967296", "0x"])
def test_legacy_ipv4_parser_rejects_invalid_numeric_hosts(bad: str) -> None:
    if bad == "0x":
        assert parse_legacy_ipv4(bad) == ipaddress.IPv4Address("0.0.0.0")  # WHATWG: empty hex is zero
        return
    with pytest.raises(ValueError):  # noqa: PT011
        parse_legacy_ipv4(bad)


def test_is_public_ip_flags_documentation_and_reserved_ranges() -> None:
    for blocked in ("192.0.2.1", "198.51.100.1", "203.0.113.5", "240.0.0.1", "2001:db8::1", "ff02::1"):
        assert not is_public_ip(ipaddress.ip_address(blocked)), blocked
    for allowed in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
        assert is_public_ip(ipaddress.ip_address(allowed)), allowed


def test_ipv4_mapped_addresses_are_judged_by_the_embedded_ipv4_address() -> None:
    # Pins the policy independently of how a given Python version treats ::ffff:0:0/96 internally.
    assert is_public_ip(ipaddress.ip_address("::ffff:8.8.8.8"))
    for private in ("::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"):
        assert not is_public_ip(ipaddress.ip_address(private)), private
    assert validate_target_url("http://[::ffff:8.8.8.8]/").ip == ipaddress.ip_address("::ffff:8.8.8.8")
