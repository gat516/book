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

## Baseline

`chaotic heavenly emperor technique` (`9305a18f-1617-4e41-a6d2-3877df98eb7e`), active graph
revision `a6622fa8` (the auto-created `legacy` revision from migration 0023 — note
`evidence-v1` is that migration's column DEFAULT, never a real prompt version):

**30.0%** — 6 correct, 11 wrong, 3 unresolved, over 20 labels.

Two failure shapes dominate, and neither is model quality:

- **Alias collapse.** One source surface claims several entities: `劳伦斯` and `阿瑞斯`
  each resolve to four, `亚巴顿` to five. Distinct characters share alias rows.
- **Glossary poisoning.** Several locked terms map to the wrong target, so resolution
  faithfully reproduces them: `阿瑞斯` (Ares) is locked to `An Ruosi`, `智慧女神`
  ("Goddess of Wisdom") to `Ling Feng`, `流萤之河` ("River of Fireflies") to `Lotus Pool`.
  Correcting these is forward-only and does not touch already-translated chapters.

The runner also reports duplicate canonicals (the same bifurcation seen from the graph
side). Five groups exist, all `artifact`, which is why a `UNIQUE` index on
`(revision_id, kind, canonical)` cannot be added until a clean rebuild.
