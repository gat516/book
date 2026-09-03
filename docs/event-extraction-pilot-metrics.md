# Chapter-event extraction pilot metrics

Recorded 2026-09-03 while evaluating the local `qwen2.5:7b-instruct`
(`Q4_K_M`) backend against the 21 translated chapters of **chaotic heavenly
emperor technique**. This is an implementation checkpoint, not an activation
approval: `novel.active_event_revision` remains `NULL`.

## Checks

- Pipeline test suite: **274 passed, 18 skipped** (the skips require optional
  local integration dependencies).
- Focused event tests: **13 passed** at the v12 checkpoint.
- The live migration `0039_structured_event_revisions.sql` was applied.

## Pilot results

| Revision | Approach | Chapter | Result | Model time |
| --- | --- | ---: | --- | ---: |
| v3 display text | One generic extraction pass, literal evidence | 7 | 11 published / 5 rejected; included false or low-value rows and missed acquisition/storage detail | ~7–8 min (2 calls) |
| v6 evidence windows | Whole chapter with 1,600-character overlapping evidence windows | 7 | 0 published / 4 rejected because evidence whitespace was stripped while retaining its offset | 307.1 s (1 call) |
| v7 exact windows | Preserved exact evidence whitespace and canonicalized accepted summaries | 7 | 2 correct rows: lotus acquisition and transfer; still missed storage | 139.8 s (1 call) |
| v8 canonical summary | Two-window batches; canonical summaries from validated roles | 7 | 4 correct rows: Ares attacks Ling Feng; Ares plucks lotuses; Ares hands one to Abadon; Abadon pockets it | 287.5 s (2 calls) |
| v11 required-role schema | Schema-derived required roles; no inference/repair | 1 | 2 correct rows: An Ruosu's detailed explanation and Ling Feng's pill handoff; missed pill administration and stabilization | 292.0 s (3 calls) |
| v12 grouped extraction | Separate physical/state, possession/item, and social/information calls | 1 | **In progress** at checkpoint: physical/state complete; possession/item running; no evaluation yet | 440.9 s for 2 completed calls |

## Findings

1. The original scarcity was partly a software failure: unresolved identities,
   overly narrow evidence, whitespace-normalized evidence offsets, unconstrained
   role shapes, and invisible streaming all caused correct or useful candidates
   to disappear.
2. The local 7B model remains the main recall constraint after those fixes. It
   can identify events in prose, but often stops early, omits a required role,
   combines multiple actions, or attributes an action to the wrong character.
3. Permissive repair/name-inference increased chapter 1 from one row to five,
   but introduced two false facts. It was therefore removed rather than traded
   for apparent coverage.
4. The active v12 experiment retains strict literal evidence and required-role
   validation while separating event groups. It must be reviewed on at least
   five chapters and meet the activation thresholds before it can be reader
   visible.

## Resource posture

- Extraction is sequential and runs only against the pinned local Ollama model.
- Each model call logs `inference_started`, 30-second `prefill`/`streaming`
  heartbeats, streamed-character count, and `inference_finished`.
- Completion results are revision- and served-model-keyed, so interrupted work
  resumes from cached calls rather than recomputing them.
- Ordinary pipeline work was paused during pilots to avoid concurrent local
  model contention.

## Activation guard

Activation remains intentionally blocked unless the completed revision has a
human review with at least 30 expected events across at least five chapters,
precision >= 0.95, plot-event recall >= 0.85, role F1 >= 0.85, status accuracy
>= 0.95, valid evidence, and zero critical false completions. These gates keep
reader-visible facts append-only, chapter-scoped, and evidence-backed (spec
§0 and §5).
