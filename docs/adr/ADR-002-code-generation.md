# ADR-002: Short-code generation — random Base62 with retry on collision

- **Status:** Accepted (proposed by AI, reviewed and arithmetic re-checked by the engineer)
- **Context:** Codes must be unique, short, hard to enumerate, and generated without cross-instance coordination (stateless, horizontally scaled app tier). Custom aliases share the same namespace.

## Options

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Random Base62, N chars, retry on unique violation** | `secrets` CSPRNG, uniform draw per character | No coordination or central counter; unguessable; trivial to reason about; collisions resolved by the DB's unique constraint | Collisions possible (rare); need a retry loop |
| B. Base62 of a hash of the URL | `base62(sha256(url+salt))[:N]` | Deterministic dedup | Truncation causes collisions anyway and needs a retry salt; we explicitly do **not** dedup (A7); leaks "has this URL been shortened" |
| C. Base62 of a sequence/Snowflake ID | Counter encoded in Base62 | No collisions | Needs a central sequence or machine-id coordination; **sequential, so enumerable** (scraping all links); couples code length to volume |

## Decision
**Option A**, 7 characters, alphabet `[0-9A-Za-z]`, drawn with `secrets.choice` (no modulo bias: choosing from a list of 62 symbols). Uniqueness is enforced **only** by the database `UNIQUE(code)` constraint: insert, and on `UniqueViolation` regenerate (max 5 attempts, then `503 service-unavailable`). Never "check then insert" (race-prone).

## Math (provisional design target N = 100 M links)
- Space: 62^7 = 3,521,614,606,208 ≈ 3.5 × 10^12 (≈ 41.7 bits).
- Probability a fresh code collides when N = 10^8 are used: 10^8 / 3.52 × 10^12 ≈ **2.8 × 10^-5**.
- Probability all 5 attempts collide: (2.8 × 10^-5)^5 ≈ 1.8 × 10^-23, i.e. effectively never.
- Expected total collisions while growing to 10^8 links (birthday estimate N² / 2M): ≈ **1,400**, each costing one extra insert.
- Unguessability: an attacker probing random codes hits a valid one with probability ≈ 2.8 × 10^-5 per request at 10^8 links; this is *unlisted*, not *private*. Rate limiting (per IP) makes bulk scraping slow; sensitive targets must not rely on code secrecy.

## Custom aliases
`^[A-Za-z0-9_-]{3,32}$`, case-sensitive, reserved words rejected (`api`, `health`, `docs`, `openapi.json`, `static`, `admin`, ...). Duplicate or previously-deleted alias → `409`. A custom alias may coincide with a future generated code; the unique constraint resolves it (generator retries).

## Consequences
- Repository layer must translate `UniqueViolation` into a domain error and distinguish alias conflict (409) from generated-code collision (retry).
- Tests: concurrency test (many parallel creates of one alias → exactly one succeeds), fake generator forcing collisions, bounded-retry exhaustion.
- If length must grow, raising to 8 chars (2.2 × 10^14) is a configuration change; existing codes remain valid.
