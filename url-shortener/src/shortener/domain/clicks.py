"""Click-event helpers: privacy-preserving derivations from request headers (ADR-004, DB_SCHEMA)."""

import hashlib
import hmac
import re
from datetime import date
from urllib.parse import urlsplit

MAX_REFERRER_LENGTH = 255
MAX_USER_AGENT_LENGTH = 255
_ORIGIN_RE = re.compile(r"^https?://[^/?#@\s]+$")  # mirrors the DB CHECK ck_clicks_referrer_origin
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_BOT_RE = re.compile(r"bot|crawler|spider|slurp|facebookexternalhit|preview|headless", re.IGNORECASE)


def referrer_origin(header: str | None) -> str | None:
    """Keep only `scheme://host[:port]`; paths, queries, fragments and userinfo are discarded.

    Anything unparseable, non-http(s), or longer than 255 characters becomes None.
    """
    if not header or len(header) > 2048 or any(c.isspace() for c in header):
        return None
    try:
        parts = urlsplit(header)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not host:
        return None
    rendered_host = f"[{host}]" if ":" in host else host
    origin = f"{parts.scheme.lower()}://{rendered_host.lower()}"
    if port is not None:
        origin = f"{origin}:{port}"
    if len(origin) > MAX_REFERRER_LENGTH or not _ORIGIN_RE.match(origin):
        return None
    return origin


def truncate_user_agent(header: str | None) -> str | None:
    if not header:
        return None
    cleaned = "".join(c for c in header if c.isprintable())
    return cleaned[:MAX_USER_AGENT_LENGTH] or None


def ua_family(user_agent: str | None) -> str:
    """Coarse family only (chrome, firefox, safari, edge, curl, bot, other)."""
    if not user_agent:
        return "other"
    lowered = user_agent.lower()
    if _BOT_RE.search(lowered):
        return "bot"
    if lowered.startswith("curl/"):
        return "curl"
    if "edg/" in lowered or "edge/" in lowered or "edga/" in lowered or "edgios/" in lowered:
        return "edge"
    if "firefox/" in lowered or "fxios/" in lowered:
        return "firefox"
    if "chrome/" in lowered or "crios/" in lowered or "opr/" in lowered:
        return "chrome"
    if "safari/" in lowered and "version/" in lowered:
        return "safari"
    return "other"


def country_from_header(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().upper()
    if not _COUNTRY_RE.match(candidate) or candidate == "XX":  # XX = unknown at the edge
        return None
    return candidate


def visitor_hash(secret: str, client_ip: str | None, user_agent: str | None, day: date) -> str | None:
    """HMAC(daily_salt, ip || ua), truncated to 16 hex chars.

    The salt changes every UTC day, so the hash cannot be reversed to an IP and cannot link one
    visitor across days.
    """
    if not client_ip:
        return None
    daily_salt = hmac.new(
        secret.encode(), f"visitor-salt:{day.isoformat()}".encode(), hashlib.sha256
    ).digest()
    message = f"{client_ip}\x1f{user_agent or ''}".encode()
    return hmac.new(daily_salt, message, hashlib.sha256).hexdigest()[:16]
