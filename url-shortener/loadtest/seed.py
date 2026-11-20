"""Create the always-cached "hot" links through the public API (idempotent: 409 means it already exists)."""

import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("LOAD_HOST", "http://app:8000")
KEY = os.environ.get("LOAD_API_KEY", "demo-key-local-dev-only")
COUNT = int(os.environ.get("HOT_LINKS", "50"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None  # surface the 302 as an HTTPError instead of following it


opener = urllib.request.build_opener(_NoRedirect)
created = existing = 0
for i in range(COUNT):
    body = json.dumps({"url": f"https://example.com/hot/{i}", "alias": f"hot-{i:03d}"}).encode()
    request = urllib.request.Request(  # noqa: S310
        f"{BASE}/api/v1/urls",
        data=body,
        method="POST",
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=10) as response:
            created += response.status == 201
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        existing += 1
    try:  # one redirect warms the cache entry
        opener.open(f"{BASE}/hot-{i:03d}", timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 302, exc.code
print(f"hot links: created={created} already_existing={existing}")
