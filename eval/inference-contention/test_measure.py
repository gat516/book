"""Guards for the only production queue mutation in the bounded experiment."""

import contextlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCES = runpy.run_path(str(Path(__file__).with_name("measure.py")))


def attempt(monkeypatch, *, catalog=("all", None), row=None, added=1):
    calls = []
    if row is None:
        row = ("ingested", False, False, None)

    class Database:
        def execute(self, sql, params=None):
            value = catalog if "worker_catalog" in sql else row
            return SimpleNamespace(fetchone=lambda: value)

        def transaction(self):
            return contextlib.nullcontext()

    class Redis:
        def eval(self, *args):
            calls.append(args)
            return added

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda *a, **kw: Database()))
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **kw: Redis())))
    monkeypatch.setenv("DATABASE_URL", "unused-test-database")
    monkeypatch.setenv("REDIS_URL", "unused-test-redis")
    context = {"account": "owner", "novel": "book", "chapter": 176}
    return calls, lambda: exec(SOURCES["ENQUEUE"], context)


@pytest.mark.parametrize("catalog", [None, ("paused", None), ("focused", "different-book")])
def test_never_overrides_queue_controls(monkeypatch, catalog):
    calls, run = attempt(monkeypatch, catalog=catalog)
    with pytest.raises(RuntimeError, match="not enabled"):
        run()
    assert calls == []


@pytest.mark.parametrize("row", [
    ("done", True, False, None),
    ("ingested", True, False, None),
    ("ingested", False, True, None),
    ("ingested", False, False, "scheduled-retry"),
    ("error", False, False, None),
])
def test_never_retranslates_or_resets_paused_failed_work(monkeypatch, row):
    calls, run = attempt(monkeypatch, row=row)
    with pytest.raises(RuntimeError, match="unpaused, unprocessed"):
        run()
    assert calls == []


def test_only_one_normal_chapter_pointer_is_requested(monkeypatch, capsys):
    calls, run = attempt(monkeypatch)
    run()
    assert len(calls) == 1
    _, numkeys, pending, processing, novel, payload = calls[0]
    assert (numkeys, pending, processing, novel) == (2, "jobs:pending", "jobs:processing", "book")
    assert json.loads(payload) == {"novel_id": "book", "chapter_index": 176,
                                  "priority": False, "enrichment": False, "retranslate": False}
    assert json.loads(capsys.readouterr().out) == {"enqueued": True, "chapter": 176}


def test_existing_queue_work_is_reported_as_not_enqueued(monkeypatch, capsys):
    _, run = attempt(monkeypatch, added=0)
    run()
    assert json.loads(capsys.readouterr().out)["enqueued"] is False


def test_all_remote_helpers_are_valid_python():
    for name in ("WATCH", "SNAPSHOT", "ENQUEUE", "ASK"):
        compile(SOURCES[name], name, "exec")
