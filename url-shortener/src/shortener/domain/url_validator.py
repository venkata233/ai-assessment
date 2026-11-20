"""URL validation and SSRF/open-redirect guard.

The service never fetches a target URL, so the real risk is being used as an open redirector to
internal or dangerous destinations (ARCHITECTURE.md section 5). Everything here is pure and offline;
the optional DNS check lives in `LinkService` behind the `DnsResolver` port.
"""

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from shortener.domain.errors import ValidationFailed

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_BLOCKED_HOSTS = frozenset({"localhost"})
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".lan",
    ".intranet",
    ".home.arpa",
)
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
_DECIMAL_RE = re.compile(r"^[0-9]+$")
_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]*$")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
# Ranges that some Python versions still report as global. Listing them here keeps the guard independent of
# the interpreter's `ipaddress` tables (a runtime upgrade or downgrade cannot silently open a hole).
_ALWAYS_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "192.0.0.0/24",  # IETF protocol assignments
        "192.88.99.0/24",  # deprecated 6to4 relay anycast
        "198.18.0.0/15",  # benchmarking
        "3fff::/20",  # documentation (RFC 9637)
    )
)
_PATH_SAFE = "/%:@!$&'()*+,;=-._~[]"
_QUERY_SAFE = _PATH_SAFE + "?"

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    host: str
    ip: IpAddress | None  # set when the host is an IP literal; None when it is a DNS name


def _fail(code: str, message: str) -> ValidationFailed:
    return ValidationFailed.single("url", code, message)


def is_public_ip(ip: IpAddress) -> bool:
    """True only for globally routable unicast addresses; unwraps addresses that embed IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            server, client = ip.teredo
            return is_public_ip(server) and is_public_ip(client)
        if ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _IPV4_COMPATIBLE:
            return False
    if any(ip in network for network in _ALWAYS_BLOCKED):
        return False
    return ip.is_global and not (
        ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
    )


def _has_forbidden_characters(value: str) -> bool:
    for char in value:
        if ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\":
            return True
        if unicodedata.category(char) in {"Cc", "Cf", "Zs", "Zl", "Zp"}:
            return True
    return False


def _parse_ipv4_number(part: str) -> int:
    if _HEX_RE.fullmatch(part):
        return int(part[2:] or "0", 16)
    if len(part) > 1 and part.startswith("0"):
        return int(part[1:], 8)  # ValueError for digits 8/9, handled by the caller
    return int(part, 10)


def parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the forms browsers accept (`127.1`, `0x7f.1`, `2130706433`, `0177.0.0.1`).

    Returns None when the host is an ordinary DNS name (its last label is not numeric). Raises
    ValueError when the host looks numeric but is not a valid address, because browsers would
    reject it too and we must not guess.
    """
    labels = host.split(".")
    if not (_DECIMAL_RE.fullmatch(labels[-1]) or _HEX_RE.fullmatch(labels[-1])):
        return None
    if len(labels) > 4 or any(not (_DECIMAL_RE.fullmatch(p) or _HEX_RE.fullmatch(p)) for p in labels):
        raise ValueError("not an IPv4 address")
    numbers = [_parse_ipv4_number(p) for p in labels]
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(labels)):
        raise ValueError("IPv4 component out of range")
    value = 0
    for number in numbers[:-1]:
        value = (value << 8) | number
    value = (value << (8 * (5 - len(labels)))) | numbers[-1]
    return ipaddress.IPv4Address(value)


def _normalise_hostname(raw_host: str) -> str:
    host = raw_host.lower().rstrip(".")
    if not host:
        raise ValueError("empty host")
    try:
        # The IDNA codec also folds look-alike dots and full-width digits, so `127。0。0。1`
        # and `①②⑦.0.0.1` are seen in their real form before the address checks run.
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid internationalised host") from exc
    ascii_host = ascii_host.lower().rstrip(".")
    if not ascii_host or len(ascii_host) > 253:
        raise ValueError("host length")
    return ascii_host


def _check_hostname(host: str) -> None:
    labels = host.split(".")
    if len(labels) < 2:
        raise _fail("host-not-allowed", "Single-label hostnames are not allowed.")
    if not all(_LABEL_RE.fullmatch(label) for label in labels):
        raise _fail("invalid-url", "The host name is not valid.")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise _fail("host-not-allowed", "Internal host names are not allowed.")


def own_origins_from_base_url(base_url: str) -> frozenset[tuple[str, int]]:
    """(host, port) pairs that count as "this service" for the redirect-loop check.

    The host is normalised exactly like target hosts are (IDNA, lower case, no trailing dot). The configured
    port plus both default ports are included, so a link to `http://host/x` is refused when the service
    itself is served at `https://host` (and the other way round): both reach the same service.
    """
    parts = urlsplit(base_url)
    host = _normalise_hostname(parts.hostname or "")
    configured = parts.port or _DEFAULT_PORTS[parts.scheme]
    return frozenset((host, port) for port in {configured, *_DEFAULT_PORTS.values()})


def validate_target_url(raw: str, *, own_origins: frozenset[tuple[str, int]] = frozenset()) -> ValidatedUrl:
    """Validate and normalise a target URL, or raise `ValidationFailed` with a precise code.

    `own_origins` holds (host, port) pairs of this service so links cannot point back at it.
    """
    if not isinstance(raw, str) or not raw:  # defensive: callers pass untrusted data
        raise _fail("invalid-url", "A URL is required.")
    if len(raw) > MAX_URL_LENGTH:
        raise _fail("url-too-long", f"The URL must be at most {MAX_URL_LENGTH} characters.")
    if _has_forbidden_characters(raw):
        raise _fail("control-characters", "The URL contains whitespace, control or backslash characters.")

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise _fail("invalid-url", "The URL could not be parsed.") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise _fail("scheme-not-allowed", "Only http and https URLs are allowed.")
    scheme = parts.scheme.lower()
    if not parts.netloc or not parts.hostname:
        raise _fail("invalid-url", "The URL must include a host.")
    if "@" in parts.netloc:
        raise _fail("credentials-not-allowed", "URLs with embedded credentials are not allowed.")
    if port == 0:
        raise _fail("invalid-url", "Port 0 is not allowed.")

    ip: IpAddress | None
    try:
        if parts.netloc.startswith("["):
            if "%" in parts.hostname:
                raise ValueError("IPv6 zone identifiers are not allowed")
            ip = ipaddress.IPv6Address(parts.hostname)
            host = ip.compressed
        else:
            ascii_host = _normalise_hostname(parts.hostname)
            ip = parse_legacy_ipv4(ascii_host)
            host = str(ip) if ip is not None else ascii_host
    except ValueError as exc:
        raise _fail("invalid-url", "The host is not a valid name or address.") from exc

    if ip is not None:
        if not is_public_ip(ip):
            raise _fail("private-address", "The target address is not publicly routable.")
    else:
        _check_hostname(host)

    effective_port = port or _DEFAULT_PORTS[scheme]
    if (host, effective_port) in own_origins:
        raise _fail("self-reference", "The URL points back at this service.")

    netloc = f"[{host}]" if isinstance(ip, ipaddress.IPv6Address) else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    normalised = urlunsplit(
        (
            scheme,
            netloc,
            quote(parts.path or "/", safe=_PATH_SAFE),
            quote(parts.query, safe=_QUERY_SAFE),
            quote(parts.fragment, safe=_QUERY_SAFE),
        )
    )
    if len(normalised) > MAX_URL_LENGTH:
        raise _fail("url-too-long", f"The normalised URL must be at most {MAX_URL_LENGTH} characters.")
    return ValidatedUrl(url=normalised, host=host, ip=ip)
