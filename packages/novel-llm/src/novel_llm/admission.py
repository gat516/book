"""Local Ollama admission across Book processes; no gateway dependency (§5.4).

POSIX advisory locks are released by the OS on process death. All clients must use
the same directory/account (or a shared volume in containers). External Ollama
clients do not participate. Never unlink a lock file: that would split its owners.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import time
from urllib.parse import urlparse

from novel_llm.provider import AdmissionRejected

_owners: dict[str, asyncio.Task] = {}


def lock_path(host: str) -> Path:
    url = urlparse(host)
    hostname = url.hostname or "localhost"
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        hostname = "loopback"
    key = f"{url.scheme}://{hostname}:{url.port or (443 if url.scheme == 'https' else 80)}"
    directory = Path(os.environ.get("BOOK_OLLAMA_LOCK_DIR", f"/tmp/book-ollama-{os.getuid()}"))
    return directory / (hashlib.sha256(key.encode()).hexdigest() + ".lock")


@asynccontextmanager
async def ollama_session(host: str, *, timeout: float = 30):
    """Reserve the endpoint for a call, or a whole benchmark in the same async task.

    Child tasks do NOT inherit ownership. Waiting never blocks worker heartbeats;
    backpressure uses the existing retry-without-failure contract.
    """
    path = lock_path(host)
    key = str(path)
    task = asyncio.current_task()
    if _owners.get(key) is task:
        yield 0.0
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    acquired = False
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise AdmissionRejected("local Ollama reserved by another Book request or benchmark", retry_after_s=5)
                await asyncio.sleep(min(0.1, max(0, timeout - (time.monotonic() - started))))
        _owners[key] = task
        yield time.monotonic() - started
    finally:
        if acquired:
            _owners.pop(key, None)
        os.close(fd)
