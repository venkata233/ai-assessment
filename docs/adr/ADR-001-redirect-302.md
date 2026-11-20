# ADR-001: Redirect with HTTP 302 (not 301)

- **Status:** Accepted (engineer decision, 2026-11-20)
- **Context:** The service redirects visitors from `/{code}` to a target URL. The status code decides who may cache the mapping. Requirements that interact with it: per-click analytics (FR-5), expiry returning `410` (AC-6), deletion taking effect immediately (AC-7).

## Options

| Option | Behaviour | Pros | Cons |
|---|---|---|---|
| `301 Moved Permanently` | Browsers, proxies, and CDNs may cache the mapping indefinitely | Fewer requests reach us; SEO treats the short link as an alias; cheaper | Repeat clicks never reach the service, so analytics undercount; a deleted or expired link keeps working for anyone who cached it; mistakes (wrong target) cannot be fixed for cached clients |
| `302 Found` | Not cached by default | Every click observable; delete/expiry effective at once; safe to correct | More traffic hits our cache/DB; no SEO link-equity transfer |
| `307/308` | Method-preserving variants | Only matter for non-GET | Not needed: redirects are GET-only |

## Decision
Use **302** and send `Cache-Control: private, max-age=0` so intermediaries and browsers re-validate. This is a product choice: accuracy and revocability over bandwidth savings.

## Consequences
- Redirect throughput relies on our own cache (ADR-005), not browser caches.
- Analytics are accurate for browser-initiated clicks.
- Revisit if the business prioritises SEO or CDN offload: a per-link opt-in `permanent` flag could be added later, with the explicit trade-off that analytics and revocation degrade for that link.
- `HEAD` requests return the same status but do not emit click events.
