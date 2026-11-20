# ADR-005: Redis cache-aside for redirects, with tombstone-based invalidation

- **Status:** Accepted
- **Context:** Redirect is ~100× more frequent than create (provisional A2: 5 k redirects/s). The lookup must be fast, must never serve a deleted link (AC-7) or an expired one (AC-6), and must survive Redis being down (AC-11).

## Decision

**Pattern:** cache-aside (read-through in the service layer), key `url:{code}`, value JSON `{"url_id": int, "target": str, "expires_at": iso|null}` or the sentinel `{"state":"absent"}` / `{"state":"deleted"}`.

| Event | Cache action |
|---|---|
| Redirect, cache hit | Use value. **Always compare `expires_at` to the clock on every hit** (a warm entry never overrides expiry, AC-6). |
| Redirect, miss | Load from PostgreSQL; populate with `SET key value NX EX ttl`. `NX` means a slow reader can never overwrite a newer write or a delete tombstone. |
| Unknown code | Negative cache `{"state":"absent"}` for 30 s (limits DB hits from scanners). |
| Create | `SET` (overwrite) the positive entry, which clears any negative entry for that code. |
| Delete | Commit to PostgreSQL first, then `SET key {"state":"deleted"} EX 3600` (a **tombstone**, not `DEL`). Tombstone + `NX` populate prevents the race "reader loaded row before delete, writes it back after `DEL`". |
| Redis error | Log, increment `cache_errors_total`, **fall back to PostgreSQL**; never fail the redirect. |

**TTL:** `min(3600 s, time_to_expiry)` ± 10 % jitter, so an entry never outlives its link and keys do not expire in lock-step.

**Eviction:** every cache key has a TTL; Redis runs `volatile-lru` (see ADR-004), protecting the analytics stream.

## Options considered
- *Write-through everywhere:* more writes, same invalidation questions; rejected (YAGNI).
- *`DEL` on delete:* simpler but has the stale-writeback race above; rejected.
- *CDN/browser caching (301):* rejected in ADR-001.
- *In-process LRU in front of Redis:* lowers latency but breaks immediate delete across instances (needs pub/sub invalidation); not in v1, listed as a future optimisation.

## Consequences
- Delete is eventually consistent only if Redis is unreachable at delete time; then an old positive entry can survive until its TTL (≤ 1 h). Mitigation: the delete path logs and increments a metric; the TTL bound limits exposure. Accepted trade-off (flagged in the risk register).
- Cache stampede on a hot key after eviction is bounded to one DB read per concurrent miss; request coalescing is deferred until load tests show it matters.
- Tests: hit, miss, negative, delete-tombstone race, expiry-on-hit with warm cache, Redis-down fallback.
