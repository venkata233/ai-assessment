# ADR-006: Owner-scoped API keys for link management

- **Status:** Accepted (engineer decision, 2026-11-20)
- **Context:** The brief lists `DELETE` and analytics endpoints without authentication. Unauthenticated, anyone could delete or inspect any link (OWASP A01 Broken Access Control).

## Decision
- Management endpoints (`POST /api/v1/urls`, `GET|DELETE /api/v1/urls/{code}`, analytics) require header `X-API-Key`.
- Keys are server-side configuration: `API_KEYS="label1:<sha256-hex>,label2:<sha256-hex>"`. Only **hashes** are configured; the plaintext key is never stored or logged. A key's **label** is its owner id and is stored in `urls.owner_key_id`.
- Verification: hash the presented key (SHA-256), compare against configured hashes with `hmac.compare_digest`. SHA-256 (not a slow password hash) is acceptable because keys are high-entropy random tokens (≥ 32 bytes), not user passwords.
- Authorisation: a caller can see or delete only links whose `owner_key_id` equals its label. Others receive **404**, not 403, so existence is not disclosed.
- Rotation: add a second hash under the same label, deploy, then remove the old one; ownership is preserved.
- Redirect and health endpoints are public; rate limiting applies to all (keyed by API-key label, otherwise client IP).

## Alternatives
- *No auth:* spec-literal, unacceptable for DELETE.
- *Per-link delete token:* simpler authZ but no way to read analytics per owner or rate-limit per tenant.
- *OAuth/JWT & user accounts:* out of scope for v1 (A12).

## Consequences / limitations
- Key issuance is manual (operator generates a random key, hashes it, adds the hash to config). Documented in the README in Phase 6.
- Labels are not a user model: no self-service, no per-key scopes, no revocation list beyond config redeploy.
- Hash configuration still counts as a secret: supplied via environment / secret manager, never committed.
