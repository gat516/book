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
| v11 on translategemma:4b | Reverted v11 prompt, same batching, translation-specialized 4B | 1 | 10 proposed (the per-batch cap) / 5 published, **all five false**: `action` filled with whole narrative clauses instead of verb phrases, and event types assigned essentially at random | 307.8 s (1 call) |
| v11 on translategemma:12b | Same again at 12.2B Q4_K_M | 1 | **Aborted, no completion.** Ollama could only load it at ctx 4096; the extraction path requests 16384, and the reload thrashed — swap-out ~64 MB/s, page cache collapsing, CPU 73% idle on IO. Killed to keep the machine usable | n/a |
| v12 grouped extraction | Separate physical/state, possession/item, and social/information calls | 1 | 7 proposed / 4 published: 2 correct rows (An Ruosu's explanation, Ling Feng's pill handoff) and 2 false rows that restate that same handoff under the `use` and `communication` frames; still missed pill administration and stabilization | 651.8 s (3 calls) |

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
4. Grouped extraction (v12) failed, and for a structural reason rather than a
   prompt one. Recall was flat against v11 (2 of 4 expected chapter-1 events),
   precision halved to 0.50, and model time more than doubled. Three of the four
   published rows describe the same pill handoff, extracted once per group under
   the `transfer`, `use`, and `communication` frames. `deduplicate_events` keys
   on `(event_type, status, arguments)`, so a restatement under a different
   frame can never collide with the row it duplicates; splitting one chapter
   across independent per-group calls made that collision-proof duplication
   inevitable. Two of those rows are false claims, not merely redundant — the
   `communication` row asserts speech that never occurred.
5. Rejected proposals **are** persisted, in `event_job.output->'rejected'` --
   an earlier draft of this document claimed they were lost, which was wrong.
   The defect was narrower: `event_rebuild.preview` never surfaced them, so the
   review report showed only what was accepted and could not answer whether the
   gate had discarded something true. It now does, matching what the graph path
   has always reported. Across every revision here, 41 of 53 rejections are
   missing roles (25) or invalid arguments (16). v12's three chapter-1
   rejections were all missing roles -- and two of them, "sketched out a simple
   map on the ground" and "gave a detailed description", are real events. The
   required-role rule is discarding true facts, not only false ones.
6. translategemma:4b is unusable here, and fails in a worse way than the 7B did.
   It is a translation-specialized tune, and it behaves like one: it satisfies
   the JSON schema and the required-role check, then fills `action` with a
   translated narrative clause rather than a verb phrase. One accepted row's
   action was a 79-character sentence carrying a semicolon and a trailing
   colon, with `event_type=acquisition` and `item="information"`. All five
   published rows were false. The 7B's errors were omissions, which the review
   gate catches cheaply; these are fabrications that pass every structural
   check, which is strictly more dangerous. Parameter count was not the
   variable that mattered -- task fit was.
7. `validate_events` has a real gap that this exposed: the only constraint on
   `action` is `max_length=80` plus "must not equal a participant surface". A
   full clause fits inside that. A structural check -- no internal sentence
   punctuation, a small word cap -- would have rejected all five rows without
   any reference to which model produced them. This is the same
   structural-not-prompted discipline (spec §0) already applied to the spoiler
   gate and glossary priming, and it is missing here.
8. The `prompt_version` string does not reliably identify the code that
   produced a revision. The v11 string never existed in any commit (`git log -S`
   finds only 4ffe56b, which introduced v12); v11 lived only in a working tree
   and was overwritten. The reverted "v11" now in the tree is a reconstruction,
   and it demonstrably differs from the original: on byte-identical chapter-1
   input (source hash 1f38a48f3422, 5 passages, 1 batch) it issues one model
   call where revision d0e3d51e recorded three. Both carry the same version
   string, so `activation_eligible` would treat d0e3d51e as current under code
   that cannot reproduce it. A version string needs to be either derived from
   the code or bumped on every behavioral change; it is currently neither.
9. Per the pre-committed decision rule, v12 missing obvious facts ends prompt
   iteration on the local 7B model. The choice is now a stronger extraction
   model versus accepting lower recall with human curation. Any revision must
   still be reviewed on at least five chapters and meet the activation
   thresholds before it can be reader visible.

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
