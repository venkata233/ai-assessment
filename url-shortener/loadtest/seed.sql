-- Cold-redirect fixtures: rows that are guaranteed to be absent from the Redis cache.
-- Run after TRUNCATE; the row count is passed with `psql -v total=300000`.
INSERT INTO urls (code, target_url, owner_key_id, is_custom_alias, created_at)
SELECT 'C' || lpad(i::text, 8, '0'), 'https://example.com/cold/' || i, 'demo', false, now()
FROM generate_series(0, :total - 1) AS i;
ANALYZE urls;
SELECT count(*) AS cold_rows FROM urls;
