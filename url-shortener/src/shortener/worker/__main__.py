"""Worker entry point: `python -m shortener.worker`."""

import asyncio

from shortener.main import run_worker

if __name__ == "__main__":
    asyncio.run(run_worker())
