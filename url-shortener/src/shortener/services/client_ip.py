"""Client IP extraction that a caller cannot spoof through X-Forwarded-For."""

import contextlib
import ipaddress


def client_ip(forwarded_for: str | None, peer: str | None, trusted_hops: int) -> str | None:
    """Return the address of the client as seen by the outermost trusted proxy.

    With N trusted proxies each appending the address it received from, the real client is the Nth
    entry from the right; anything further left was supplied by the client and is ignored. With
    N = 0 the header is not trusted at all and the socket peer is used.
    """
    if trusted_hops > 0 and forwarded_for:
        entries = [part.strip() for part in forwarded_for.split(",")]
        if len(entries) >= trusted_hops:
            with contextlib.suppress(ValueError):  # malformed entry: fall back to the socket peer
                return str(ipaddress.ip_address(entries[-trusted_hops]))
    return peer
