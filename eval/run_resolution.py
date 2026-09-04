#!/usr/bin/env python3
"""Entity-resolution accuracy (instructions.md §5, §12 risk #2; PLAN.md workstream A).

Resolution drift is the highest-impact *silent* failure in this system: nothing crashes,
the graph just slowly bifurcates entities, and every downstream feature is quietly
corrupt by chapter 600. The only defense is a number you actually look at.

This measures the **stored result** of resolution rather than re-running the resolver:
for each labeled mention, it asks the graph "which entity does this surface resolve to?"
and compares that entity's canonical name to the label. That means it costs nothing, runs
in seconds, and can be run after any pipeline change — which is the point, because a
metric that needs an LLM budget is a metric nobody runs.

It reports three outcomes, and they fail differently:

  correct     — the surface resolves to the expected entity
  wrong       — it resolves to a DIFFERENT entity (two entities where there should be
                one, or a mention attached to the wrong one) — this is drift
  unresolved  — no entity claims the surface at all; the mention was dropped

Usage:
    python eval/run_resolution.py --novel-id <uuid> [--file eval/mentions.jsonl]
                                  [--min-accuracy 0.9]

Label format, one JSON object per line:
    {"chapter": 41, "surface": "Azure Sect", "expected_canonical": "Azure Cloud Sect"}

Add ~20 whenever you are testing resolution anyway; a few hundred is the target. Labels
key on the canonical NAME, not the entity id, because ids are generated per run and a
re-ingest would invalidate the whole file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import psycopg

DEFAULT_FILE = Path(__file__).parent / "mentions.jsonl"
DEFAULT_DB = "postgres://engine:engine@localhost:5432/novel_engine"


def load_labels(path: Path) -> list[dict]:
    if not path.exists():
        return []
    labels = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            labels.append(json.loads(line))
    return labels


def active_graph_revision(conn, novel_id: str) -> str:
    """The revision this novel currently serves to readers."""
    with conn.cursor() as cur:
        cur.execute("SELECT active_graph_revision FROM novel WHERE id = %s", (novel_id,))
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"novel {novel_id} not found")
    if not row[0]:
        raise SystemExit(
            f"novel {novel_id} has no active graph revision; pass --revision-id to score a "
            "staging candidate"
        )
    return str(row[0])


def duplicate_canonicals(conn, novel_id: str, revision_id: str) -> list[tuple[str, str, int]]:
    """Canonical names carried by more than one entity in this revision.

    The same defect `resolve_surface` reports, seen from the other side. That function
    catches it per labeled mention ("this surface claims four entities"); this one catches
    it across the whole graph, including concepts nobody thought to label. Reported rather
    than enforced: a UNIQUE index on (revision_id, kind, canonical) is the real fix, but it
    cannot be created while duplicates exist, so this is the detector that says when it can.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT canonical, kind, count(*)
            FROM entity
            WHERE novel_id = %s AND revision_id = %s
            GROUP BY canonical, kind
            HAVING count(*) > 1
            ORDER BY count(*) DESC, canonical
            """,
            (novel_id, revision_id),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def resolve_surface(conn, novel_id: str, revision_id: str, surface: str) -> list[str]:
    """Canonical names of every entity claiming this surface, via alias or canonical.

    Returns a LIST, not one name, because more than one hit is itself the finding: it
    means the surface is ambiguous in the graph, which is the shape entity bifurcation
    takes when you look at it from the mention side.

    Scoped to ONE revision. Without that filter every staging and archived rebuild joins
    the result set, so a leftover experimental revision turns `correct` into `wrong` with
    no drift having occurred -- and comparing models is exactly when spare revisions exist.
    Mirrors the scoping production reads already use (askai/retrieval.py).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT e.canonical
            FROM entity e
            LEFT JOIN alias a ON a.entity_id = e.id
            WHERE e.novel_id = %s AND e.revision_id = %s
              AND (a.surface = %s OR e.canonical = %s)
            """,
            (novel_id, revision_id, surface, surface),
        )
        return [r[0] for r in cur.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--novel-id", required=True)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DB))
    parser.add_argument(
        "--revision-id",
        default=None,
        help="score this graph revision instead of the novel's active one",
    )
    parser.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        help="exit non-zero below this, so CI can treat drift as a failing test",
    )
    args = parser.parse_args()

    labels = load_labels(args.file)
    if not labels:
        print(f"no labels in {args.file} — add some while testing resolution (workstream A)")
        # An unmeasured metric must not read as a passing one. A caller that asked for a
        # floor asked for a guarantee, and zero labels cannot supply it -- returning 0 here
        # is how CI stays green while measuring nothing.
        return 1 if args.min_accuracy is not None else 0

    outcomes: Counter[str] = Counter()
    failures: list[str] = []

    with psycopg.connect(args.database_url) as conn:
        revision_id = args.revision_id or active_graph_revision(conn, args.novel_id)
        print(f"graph revision: {revision_id}")
        for label in labels:
            surface = label["surface"]
            expected = label["expected_canonical"]
            found = resolve_surface(conn, args.novel_id, revision_id, surface)

            if not found:
                outcomes["unresolved"] += 1
                failures.append(f"  unresolved  ch{label.get('chapter', '?')}  {surface!r}")
            elif found == [expected]:
                outcomes["correct"] += 1
            else:
                outcomes["wrong"] += 1
                failures.append(
                    f"  wrong       ch{label.get('chapter', '?')}  {surface!r}"
                    f" -> {found} (expected {expected!r})"
                )

        duplicates = duplicate_canonicals(conn, args.novel_id, revision_id)

    total = sum(outcomes.values())
    accuracy = outcomes["correct"] / total

    if duplicates:
        print("duplicate canonicals (one name, several entities — bifurcation from the")
        print("graph side; blocks a UNIQUE index on (revision_id, kind, canonical)):")
        for canonical, kind, count in duplicates:
            print(f"  {count}x  {kind:<10} {canonical!r}")
        print()

    if failures:
        print("failures:")
        print("\n".join(failures))
        print()
    print(
        f"resolution accuracy: {accuracy:.1%}  "
        f"({outcomes['correct']} correct, {outcomes['wrong']} wrong, "
        f"{outcomes['unresolved']} unresolved, {total} labeled)"
    )

    if args.min_accuracy is not None and accuracy < args.min_accuracy:
        print(f"FAIL: below --min-accuracy {args.min_accuracy:.1%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
