# ADR-003: PostgreSQL (SQL) as system of record; soft delete as alias tombstone

- **Status:** Accepted (engineer decision: PostgreSQL + Redis)
- **Context:** We store links (write-light, read-heavy, needs strict uniqueness of codes/aliases) and click events (append-heavy, aggregated by link and time).

## Options

| Option | Pros | Cons |
|---|---|---|
| **PostgreSQL** | Unique constraints give race-free alias/code uniqueness; transactions; rich aggregation SQL for analytics; migrations; ubiquitous tooling; ≈100 M link rows is comfortably within one node (rough estimate: ~100–200 bytes per row plus indexes ⇒ tens of GB, unverified) | Vertical scaling limits for the click table; sharding is manual |
| DynamoDB / Cassandra (KV) | Horizontal scale for key lookups; ideal for pure redirect lookup | Uniqueness of custom aliases needs conditional writes/LWT; analytics aggregation needs a second system; heavier ops/vendor coupling |
| MongoDB | Flexible schema | Little benefit here; unique index OK but analytics aggregation and ops story not better than PG |
| ClickHouse for clicks | Excellent for analytics at scale | A second datastore on day one is over-engineering for v1 |

## Decision
- **PostgreSQL 16** for both `urls` and `clicks` (schema in `docs/DB_SCHEMA.md`, migrations in `url-shortener/migrations`).
- The redirect hot path is served from Redis (ADR-005), so PostgreSQL read load is only cache misses.
- **Soft delete** (`deleted_at`) keeps the row, so `UNIQUE(code)` doubles as a permanent tombstone (Phase 1 decision 4): a deleted code can never be re-registered. Hard deletes are blocked for rows that have clicks (`ON DELETE RESTRICT`).
- `code` uses `COLLATE "C"` so comparison is byte-wise and case-sensitive on any database default.
- Clicks use the Redis Stream entry id as the primary key, which makes redelivery idempotent (ADR-004).
- `urls.click_count` is a denormalised counter maintained by the analytics worker so metadata reads never `COUNT(*)` the clicks table.

## Consequences / upgrade path (not built in v1)
1. Read replicas for cache-miss lookups and analytics queries.
2. `clicks` range-partitioned by month (drop partitions for retention) once the table passes roughly 10^8 rows (a rule of thumb, to be re-measured).
3. Daily rollup table (`clicks_daily(url_id, day, clicks, ...)`) maintained by the worker, with shorter raw retention, or a columnar store such as ClickHouse.

**Engineer decision (Phase 2 approval): v1 keeps raw click rows only.** Partitioning and rollups are *future improvements*, documented here and not built. Analysis (not measured): at the provisional peak of 5 k clicks/s, raw per-click rows in a single PostgreSQL node would not be sustainable for 90 days, so v1 makes **no claim** about that target. The provisional 5,000 redirects/s figure is a design input only; it may be reported as achieved only with Locust evidence from Phase 4, and the report must state the hardware used.
4. Sharding `urls` by hash(code) if a single primary is outgrown.

## Risks accepted
Hot-row contention on `urls.click_count` for viral links: mitigated by the worker applying one aggregated `UPDATE` per link per batch, not per click.
