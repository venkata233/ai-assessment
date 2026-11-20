"""System adapters: wall clock and DNS."""

import asyncio
import socket
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SystemDnsResolver:
    """Resolves a host with the OS resolver. Any failure yields an empty list (the check is advisory)."""

    def __init__(self, timeout_seconds: float = 2.0) -> None:
        self._timeout = timeout_seconds

    async def resolve(self, host: str) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), self._timeout
            )
        except (TimeoutError, OSError):
            return []
        return sorted({str(info[4][0]).split("%")[0] for info in infos})
