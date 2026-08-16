# eval — entity-resolution accuracy

Workstream A from the build plan, and the tripwire for §12's highest-impact *silent*
failure. Resolution drift does not crash anything: the graph just slowly bifurcates
entities ("Azure Cloud Sect" / "Blue Cloud Sect" / "Qingyun Sect" become three) and every
downstream feature is quietly corrupt by chapter 600. You will not notice from the UI.

## The habit

Whenever you are testing resolution anyway, save ~20 labeled mentions. It costs minutes
and it is the only way to *measure* accuracy instead of vibing it. A few hundred labels
is the target; the point is that the number exists from the first resolve change, not
that it starts large.

## `mentions.jsonl`

One JSON object per line. Lines starting with `//` are ignored.

```json
{"chapter": 41, "surface": "Azure Sect", "expected_canonical": "Azure Cloud Sect"}
```

- `surface` — the name exactly as the chapter wrote it.
- `expected_canonical` — the canonical name of the entity it *should* resolve to.
- `chapter` — where it appeared, for reading failure output.

Labels key on the canonical **name**, never an entity id: ids are generated per run, so
an id-keyed file would be invalidated by any re-ingest.

## Running it

```bash
python eval/run_resolution.py --novel-id <uuid>
python eval/run_resolution.py --novel-id <uuid> --min-accuracy 0.9   # for CI
```

Run it whenever a prompt, model, or resolve change lands, and treat a drop as a
regression like any failing test. Three outcomes, failing differently:

| outcome | meaning |
|---|---|
| `correct` | the surface resolves to the expected entity |
| `wrong` | it resolves to a *different* entity — this is drift |
| `unresolved` | no entity claims the surface; the mention was dropped |

`unresolved` also shows up at ingest time as graph-write's `unresolved=` count, which is
the same signal seen from the writing side.
