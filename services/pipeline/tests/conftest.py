"""Shared test fixtures. DB-touching tests (marked ``db``) need a live Postgres — the
``db_conn`` fixture skips them cleanly when one isn't reachable, so the pure suite
(test_idempotency, test_provider, test_chunk) keeps running standalone."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgres://engine:engine@localhost:5432/novel_engine"
)


@pytest_asyncio.fixture
async def db_conn():
    import psycopg

    try:
        conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    except Exception as exc:  # noqa: BLE001 — any connection failure means "skip"
        pytest.skip(f"no live Postgres at {DATABASE_URL}: {exc}")
    try:
        yield conn
    finally:
        await conn.close()
