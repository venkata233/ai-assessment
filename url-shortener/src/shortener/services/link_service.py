"""Link use cases: create, resolve (redirect), get, delete. Depends on ports only."""

import ipaddress
import random
from dataclasses import dataclass
from datetime import datetime

import structlog

from shortener.domain.codes import CodeGenerator, validate_alias
from shortener.domain.errors import (
    AliasConflict,
    CodeGenerationExhausted,
    CodeTaken,
    FieldIssue,
    LinkExpired,
    LinkNotFound,
    ValidationFailed,
)
from shortener.domain.expiry import validate_expiry
from shortener.domain.models import Link
from shortener.domain.url_validator import ValidatedUrl, is_public_ip, validate_target_url
from shortener.metrics import Metrics
from shortener.ports import CacheEntry, Clock, DnsResolver, LinkCache, LinkRepository

MAX_GENERATION_ATTEMPTS = 5
TOMBSTONE_TTL_SECONDS = 3600
_log = structlog.get_logger()


@dataclass(frozen=True)
class Resolved:
    url_id: int
    target: str


class LinkService:
    def __init__(
        self,
        *,
        links: LinkRepository,
        cache: LinkCache,
        clock: Clock,
        generator: CodeGenerator,
        dns: DnsResolver,
        metrics: Metrics,
        own_origins: frozenset[tuple[str, int]],
        cache_ttl_seconds: int = 3600,
        negative_cache_ttl_seconds: int = 30,
        dns_check_enabled: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._links = links
        self._cache = cache
        self._clock = clock
        self._generator = generator
        self._dns = dns
        self._metrics = metrics
        self._own_origins = own_origins
        self._cache_ttl = cache_ttl_seconds
        self._negative_ttl = negative_cache_ttl_seconds
        self._dns_check = dns_check_enabled
        self._rng = rng or random.SystemRandom()

    # ----- create -----------------------------------------------------------------------------

    async def create(self, *, owner: str, url: str, alias: str | None, expires_at: datetime | None) -> Link:
        now = self._clock.now()
        issues: list[FieldIssue] = []
        validated: ValidatedUrl | None = None
        expiry: datetime | None = None
        try:
            validated = validate_target_url(url, own_origins=self._own_origins)
        except ValidationFailed as exc:
            issues.extend(exc.issues)
        if alias is not None:
            try:
                validate_alias(alias)
            except ValidationFailed as exc:
                issues.extend(exc.issues)
        try:
            expiry = validate_expiry(expires_at, now)
        except ValidationFailed as exc:
            issues.extend(exc.issues)
        if issues or validated is None:
            raise ValidationFailed(issues)

        await self._check_dns(validated)

        attempts = 1 if alias is not None else MAX_GENERATION_ATTEMPTS
        link: Link | None = None
        for _ in range(attempts):
            code = alias if alias is not None else self._generator.generate()
            try:
                link = await self._links.insert(
                    code=code,
                    target_url=validated.url,
                    owner_key_id=owner,
                    is_custom_alias=alias is not None,
                    created_at=now,
                    expires_at=expiry,
                )
                break
            except CodeTaken:
                if alias is not None:
                    raise AliasConflict from None
                _log.warning("code_collision", attempt_limit=attempts)
        if link is None:
            raise CodeGenerationExhausted

        # Overwrite (not NX): clears any negative entry cached for this code.
        entry = CacheEntry("active", link.id, link.target_url, link.expires_at)
        ttl = self._positive_ttl(link.expires_at, now)
        if ttl is not None:
            await self._cache.put(link.code, entry, ttl)
        return link

    async def _check_dns(self, validated: ValidatedUrl) -> None:
        if not self._dns_check or validated.ip is not None:
            return
        for address in await self._dns.resolve(validated.host):
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if not is_public_ip(ip):
                raise ValidationFailed.single(
                    "url", "private-address", "The target host resolves to a non-public address."
                )

    # ----- redirect ---------------------------------------------------------------------------

    async def resolve(self, code: str) -> Resolved:
        """Cache-aside lookup. Expiry is compared with the clock on every hit, warm cache or not."""
        now = self._clock.now()
        entry = await self._cache.get(code)
        if entry is not None:
            self._metrics.inc("cache_hits_total")
            return self._from_entry(entry, now)
        self._metrics.inc("cache_misses_total")

        link = await self._links.get_by_code(code)
        if link is None:
            await self._cache.put_if_absent(code, CacheEntry("absent"), self._negative_ttl)
            raise LinkNotFound
        if link.deleted_at is not None:
            await self._cache.put_if_absent(code, CacheEntry("deleted"), TOMBSTONE_TTL_SECONDS)
            raise LinkNotFound
        if link.is_expired(now):
            raise LinkExpired
        ttl = self._positive_ttl(link.expires_at, now)
        if ttl is not None:
            active = CacheEntry("active", link.id, link.target_url, link.expires_at)
            await self._cache.put_if_absent(code, active, ttl)
        return Resolved(url_id=link.id, target=link.target_url)

    @staticmethod
    def _from_entry(entry: CacheEntry, now: datetime) -> Resolved:
        if entry.state != "active" or entry.url_id is None or entry.target is None:
            raise LinkNotFound
        if entry.expires_at is not None and entry.expires_at <= now:
            raise LinkExpired
        return Resolved(url_id=entry.url_id, target=entry.target)

    def _positive_ttl(self, expires_at: datetime | None, now: datetime) -> int | None:
        """min(cache TTL with +-10 % jitter, time to expiry); None when it is not worth caching."""
        base = int(self._cache_ttl * self._rng.uniform(0.9, 1.1))
        if expires_at is None:
            return max(1, base)
        remaining = int((expires_at - now).total_seconds())
        if remaining <= 0:
            return None
        return max(1, min(base, remaining))

    # ----- management -------------------------------------------------------------------------

    async def get(self, *, owner: str, code: str) -> Link:
        link = await self._links.get_by_code(code)
        if link is None or link.deleted_at is not None or link.owner_key_id != owner:
            raise LinkNotFound
        return link

    async def delete(self, *, owner: str, code: str) -> None:
        await self.get(owner=owner, code=code)
        deleted = await self._links.soft_delete(code=code, owner_key_id=owner, deleted_at=self._clock.now())
        if not deleted:  # lost a race with a concurrent delete
            raise LinkNotFound
        # Commit first, then write a tombstone (not DEL): see ADR-005 for the stale write-back race.
        await self._cache.put(code, CacheEntry("deleted"), TOMBSTONE_TTL_SECONDS)
