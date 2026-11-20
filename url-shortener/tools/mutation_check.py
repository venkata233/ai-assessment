"""Manual mutation sanity check (T15): break the code on purpose in a scratch copy; tests must fail.

Usage (from the url-shortener folder, with the dev requirements installed): python tools/mutation_check.py
Nothing in the real project is modified: mutations are applied to a copy under the system temp folder.
Resource-heavy: it runs the unit suite once per mutant, and the integration mutants (REQUIRE_INTEGRATION=1) need
PostgreSQL and Redis (`docker compose up -d --wait postgres redis`). It has not been run since the Phase 4 changes.

Reporting rules (so the score cannot be inflated):
- KILLED    the mutant was applied and its tests failed, having passed on the unmutated copy.
- SURVIVED  the mutant was applied and its tests still passed.
- SKIPPED   the mutant could not be applied exactly as defined (its text is no longer in the source), or its tests
            already fail without the mutation (for example PostgreSQL or Redis is not running). A skipped mutant is
            never counted as killed: the score is killed / (killed + survived), and skipped mutants are listed.
Exit code: 2 baseline fails, 1 any survivor, 3 no survivor but at least one skipped mutant, 0 all applied and killed.

`tests/unit/test_mutation_tool.py` checks, without running any mutant, that every pattern below still matches the source,
so a refactor that makes a mutant stale fails an ordinary test run.

The 20 of 22 original mutants killed (docs/AI_USAGE_LOG.md entry 026) is a Phase 3 result. The last five mutants were
added after the Phase 4 security fixes and have never run. Mutants 3 and 7 were re-targeted to the current source in
Phase 6, and mutant 27 was sharpened so it no longer duplicates mutant 3; none of the three has been run either.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


@dataclass
class Mutation:
    name: str
    file: str
    old: str
    new: str
    tests: list[str]
    also: tuple[str, str] | None = None  # a second replacement in the same file


MUTATIONS = [
    Mutation("warm cache overrides expiry", "src/shortener/services/link_service.py",
             "if entry.expires_at is not None and entry.expires_at <= now:", "if False:", ["tests/unit"]),
    Mutation("expiry boundary inclusive", "src/shortener/domain/models.py",
             "self.expires_at <= now", "self.expires_at < now", ["tests/unit"]),
    Mutation("delete writes no tombstone (the write and its failure handling both gone)", "src/shortener/services/link_service.py",
             'if not await self._cache.put(code, CacheEntry("deleted"), TOMBSTONE_TTL_SECONDS):', "if False:", ["tests/unit"]),
    Mutation("cache populate overwrites (stale write-back race)", "src/shortener/services/link_service.py",
             "await self._cache.put_if_absent(code, active, ttl)", "await self._cache.put(code, active, ttl)", ["tests/unit"]),
    Mutation("no forbidden-character check", "src/shortener/domain/url_validator.py",
             "if _has_forbidden_characters(raw):", "if False:", ["tests/unit"]),
    Mutation("IPv4-mapped IPv6 not unwrapped", "src/shortener/domain/url_validator.py",
             "if ip.ipv4_mapped is not None:", "if False:", ["tests/unit"]),
    Mutation("legacy numeric IPv4 forms accepted as names", "src/shortener/domain/url_validator.py",
             "if not (_DECIMAL_RE.fullmatch(labels[-1]) or _HEX_RE.fullmatch(labels[-1])):", "if True:", ["tests/unit"]),
    Mutation("userinfo allowed", "src/shortener/domain/url_validator.py",
             'if "@" in parts.netloc:', "if False:", ["tests/unit"]),
    Mutation("non-crypto random for codes", "src/shortener/domain/codes.py",
             "secrets.choice(BASE62)", "__import__('random').choice(BASE62)", ["tests/unit"]),
    Mutation("plain == for API keys", "src/shortener/services/auth.py",
             "hmac.compare_digest(digest, known_digest)", "digest == known_digest", ["tests/unit"]),
    Mutation("rate limit off by one", "src/shortener/services/rate_limiter.py",
             "if count > limit:", "if count >= limit:", ["tests/unit"]),
    Mutation("rate limiter fails closed", "src/shortener/services/rate_limiter.py",
             "            return\n        if count > limit:", "            raise RateLimited(1)\n        if count > limit:", ["tests/unit"]),
    Mutation("referrer path stored", "src/shortener/api/routes.py",
             'referrer=referrer_origin(request.headers.get("referer"))', 'referrer=request.headers.get("referer")', ["tests/unit"]),
    Mutation("XFF trusted blindly (leftmost entry, spoofable)", "src/shortener/services/client_ip.py",
             "entries[-trusted_hops]", "entries[0]", ["tests/unit"]),
    Mutation("non-owner can read", "src/shortener/services/link_service.py",
             "or link.owner_key_id != owner", "", ["tests/unit"]),
    Mutation("HEAD counted as click", "src/shortener/api/routes.py",
             'if request.method == "GET":', "if True:", ["tests/unit"]),
    Mutation("worker never ACKs", "src/shortener/worker/consumer.py",
             "await self._redis.xack(self._stream, self._group, *[m[0] for m, _ in valid])", "pass", ["tests/integration/test_worker.py"]),
    Mutation("insert is not idempotent", "src/shortener/infra/repositories.py",
             "ON CONFLICT (event_id) DO NOTHING", "", ["tests/integration/test_repositories.py"]),
    Mutation("counter bumped for every row (double counts redelivery)", "src/shortener/infra/repositories.py",
             "SELECT url_id, count(*) AS n FROM ins GROUP BY url_id",
             "SELECT url_id, count(*) AS n FROM unnest(CAST(:url_ids AS bigint[])) AS t(url_id) GROUP BY url_id",
             ["tests/integration/test_repositories.py", "tests/integration/test_worker.py"]),
    Mutation("redirect awaits the click publish inline", "src/shortener/api/routes.py",
             "background.add_task(container.publisher.publish, record)", "await container.publisher.publish(record)",
             ["tests/unit"]),
    Mutation("lock order removed AND retries removed (deadlock exposure)", "src/shortener/infra/repositories.py",
             "await conn.execute(_LOCK_URLS, {\"ids\": sorted({e.url_id for e in events})})", "pass",
             ["tests/integration/test_repositories.py::test_overlapping_concurrent_batches_neither_deadlock_nor_miscount"],
             also=("_MAX_BATCH_ATTEMPTS = 4", "_MAX_BATCH_ATTEMPTS = 1")),
    Mutation("retries removed only (the lock alone should be enough: expected to SURVIVE)", "src/shortener/infra/repositories.py",
             "_MAX_BATCH_ATTEMPTS = 4", "_MAX_BATCH_ATTEMPTS = 1",
             ["tests/integration/test_repositories.py::test_overlapping_concurrent_batches_neither_deadlock_nor_miscount"]),
    # Phase 4 security fixes (defined after the review; NOT yet run - see docs/VALIDATION.md section 2).
    Mutation("trailing newline accepted in codes (match instead of fullmatch)", "src/shortener/domain/codes.py",
             "CODE_PATTERN.fullmatch(code)", "CODE_PATTERN.match(code)", ["tests/unit"]),
    Mutation("self-reference ignores the other scheme's default port", "src/shortener/domain/url_validator.py",
             "for port in {configured, *_DEFAULT_PORTS.values()}", "for port in {configured}", ["tests/unit"]),
    Mutation("geo header trusted from any client", "src/shortener/api/routes.py",
             "if settings.trust_geo_header", "if True", ["tests/unit"]),
    Mutation("dev credentials guard disabled", "src/shortener/config.py",
             "        if found:\n", "        if False:\n", ["tests/unit"]),
    Mutation("tombstone failure not reported (the write still happens)", "src/shortener/services/link_service.py",
             'if not await self._cache.put(code, CacheEntry("deleted"), TOMBSTONE_TTL_SECONDS):',
             'if not await self._cache.put(code, CacheEntry("deleted"), TOMBSTONE_TTL_SECONDS) and False:', ["tests/unit"]),
]


def apply_mutation(m: Mutation, original: str) -> str | None:
    """The mutated source, or None when the mutant cannot be applied exactly as defined (it must then be SKIPPED)."""
    if m.old == m.new or m.old not in original:
        return None
    mutated = original.replace(m.old, m.new, 1)
    if m.also is not None:
        if m.also[0] not in mutated:
            return None
        mutated = mutated.replace(m.also[0], m.also[1], 1)
    return None if mutated == original else mutated


def summarise(killed: list[str], survived: list[str], skipped: list[str]) -> tuple[str, int]:
    """The summary line and the exit code. Skipped mutants are never part of the killed count or the score."""
    applied = len(killed) + len(survived)
    score = f"{len(killed)}/{applied} applied mutants killed" + (f" ({100 * len(killed) // applied}%)" if applied else "")
    text = f"{score}; survivors: {survived}; skipped (not evaluated, not counted as killed): {skipped}"
    return text, (1 if survived else 3 if skipped else 0)


def run(workdir: Path, tests: list[str]) -> bool:
    """Return True when the suite FAILS (the mutant is killed)."""
    env = {**os.environ, "REQUIRE_INTEGRATION": "1"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
        cwd=workdir, env=env, capture_output=True, text=True, check=False,
    )
    return result.returncode != 0


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="mutants-")) / "url-shortener"
    shutil.copytree(PROJECT, scratch, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".coverage"))
    print(f"scratch copy: {scratch}")
    baseline_failed = run(scratch, ["tests/unit", "tests/contract"])
    print(f"baseline (unmutated) fails: {baseline_failed}  <- must be False")
    if baseline_failed:
        return 2
    passes_unmutated = {("tests/unit",): True}  # a subset of the baseline that just passed
    killed: list[str] = []
    survived: list[str] = []
    skipped: list[str] = []
    for m in MUTATIONS:
        path = scratch / m.file
        original = path.read_text(encoding="utf-8")
        mutated = apply_mutation(m, original)
        if mutated is None:
            print(f"SKIPPED  {m.name}: pattern not found in the source, so it was not applied")
            skipped.append(m.name)
            continue
        key = tuple(m.tests)
        if key not in passes_unmutated:
            passes_unmutated[key] = not run(scratch, list(key))
        if not passes_unmutated[key]:
            print(f"SKIPPED  {m.name}: its tests already fail without the mutation (is PostgreSQL/Redis running?)")
            skipped.append(m.name)
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            failed = run(scratch, m.tests)
        finally:
            path.write_text(original, encoding="utf-8")
        print(f"{'KILLED  ' if failed else 'SURVIVED'} {m.name}")
        (killed if failed else survived).append(m.name)
    shutil.rmtree(scratch.parent, ignore_errors=True)
    text, code = summarise(killed, survived, skipped)
    print(f"\n{text}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
