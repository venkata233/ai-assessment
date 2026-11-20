"""API-key authentication (ADR-006): SHA-256 digest compared in constant time against configured hashes."""

import hashlib
import hmac
from collections.abc import Mapping

from shortener.domain.errors import Unauthorized

_MAX_KEY_LENGTH = 256


class ApiKeyAuthenticator:
    def __init__(self, hashes: Mapping[str, str]) -> None:
        self._hashes = dict(hashes)  # sha256 hex -> owner label

    def authenticate(self, presented: str | None) -> str:
        """Return the owner label for a valid key, otherwise raise Unauthorized."""
        if not presented or len(presented) > _MAX_KEY_LENGTH:
            raise Unauthorized
        digest = hashlib.sha256(presented.encode()).hexdigest()
        owner: str | None = None
        for known_digest, label in self._hashes.items():
            # No early exit: the loop time does not depend on which key (if any) matched.
            if hmac.compare_digest(digest, known_digest):
                owner = label
        if owner is None:
            raise Unauthorized
        return owner
