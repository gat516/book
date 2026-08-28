"""Stage 3: RESOLVE (instructions.md §5 step 3, "Entity resolution"; §12 risk #2; PLAN.md 1.6).

Turns the names a chapter uses into entity ids, by **retrieve-then-resolve**: pull
candidate entities, then let the LLM confirm or disambiguate *against those candidates
only*. Free-generating a canonical name per chapter is what drifts — "Azure Cloud Sect",
"Blue Cloud Sect" and "Qingyun Sect" become three entities by chapter 600, nothing
errors, and every downstream feature is quietly corrupt. This stage replaces the
exact-match placeholder that stood in for it through 1.5.

Three passes, cheapest first:

1. **Scanned exact hits** (free). ``state.mentions`` already carries entity ids for every
   known alias found in the text. A surface matching exactly one entity is resolved
   without asking anything.
2. **Proposal** (one LLM call per chapter). Aho-Corasick can only match aliases the graph
   already has, so a character introduced in this chapter is invisible to it. The
   proposal pass reads the chapter and names the surfaces that look like entities. This
   is the only generative step, and it proposes *strings from the text*, not identities.
3. **Disambiguation** (one LLM call per unresolved surface). Retrieve candidates by exact
   match plus vector similarity, then choose. ``parse_decision`` rejects any id that was
   not offered, so "never free-generates" is enforced by the parser rather than requested
   by the prompt.

**Deliberately not content-cached** (§3.5), and this looks like an omission next to
state-extract's careful two-level check, so: resolve's output depends on the live alias
index, not just chapter text — the same chapter resolved before and after another
chapter runs can legitimately differ. It also needs no ``job_is_done`` guard, because
entity and alias writes are ``ON CONFLICT DO NOTHING`` upserts and are therefore
naturally idempotent. State-extract needed that guard only because facts are append-only
INSERTs, where a re-run duplicates rather than no-ops. It still takes a ``job`` row for
tracking; ``jobs.LLM_STAGES`` already lists it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid

from pipeline.context import PipelineState, StageContext
from pipeline.graph import AliasRow, CandidateRow, EntityRow, GraphWriter
from pipeline.jobs import idempotency_key, insert_job, mark_job_done, model_for_stage
from pipeline.llm.provider import Class
from pipeline.resolution import (
    NEW_ENTITY,
    Candidate,
    FreeGeneratedEntity,
    build_disambiguation_system_prompt,
    build_disambiguation_user_prompt,
    build_proposal_system_prompt,
    build_proposal_user_prompt,
    parse_decision,
    parse_proposal,
)

log = logging.getLogger(__name__)

STAGE = "resolve"

CANDIDATE_K = 5  # nearest neighbours offered alongside the exact matches
CONTEXT_WINDOW = 120  # characters either side of the first occurrence, for disambiguation
UNKNOWN_KIND = "unknown"

# How many DISTINCT chapters must independently propose the same source→target mapping
# before it is locked (migration 0016). 1 disables corroboration and restores the old
# lock-on-first-sight behaviour.
GLOSSARY_MIN_PROPOSALS = 2

# A name should not run to a sentence; a model that returns one has misunderstood the ask.
MAX_TARGET_TERM_CHARS = 80

_CJK_RANGE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK_LANGS = frozenset({"zh", "ja"})


def _target_term_problem(target_term: str, target_lang: str) -> str | None:
    """Return why this target term is unusable, or None if it looks plausible.

    Cheap shape checks, not a quality judgement — they catch a model that has failed to
    translate at all rather than one that translated badly. The distinction matters because
    a locked term is immutable and enforced against every later translation, so garbage
    here is unrecoverable while a merely mediocre name is not.
    """
    target = target_term.strip()
    if len(target) > MAX_TARGET_TERM_CHARS:
        return f"looks like prose, not a name ({len(target)} chars)"
    if target_lang not in _CJK_LANGS and _CJK_RANGE.search(target):
        # The commonest weak-model failure for zh→en: echoing the source back, or emitting
        # a placeholder name, instead of rendering it in the target language.
        return f"contains CJK characters but target language is {target_lang!r}"
    return None


async def _target_term_owner(db, novel_id: str, target_term: str) -> str | None:
    """Which source term already claims this target in this novel, if any."""
    row = await (
        await db.execute(
            "SELECT source_term FROM glossary WHERE novel_id = %s AND target_term = %s AND NOT deleted",
            (novel_id, target_term),
        )
    ).fetchone()
    return row[0] if row else None


async def _record_candidate(
    db, novel_id: str, source_term: str, target_term: str, chapter: int
) -> int:
    """Record a proposed mapping and return how many distinct chapters have proposed it.

    The counter advances only when the proposing chapter differs from the last one seen, so
    re-running a single chapter can never corroborate its own suggestion — "independent"
    has to mean independent for this guard to be worth anything.
    """
    row = await (
        await db.execute(
            """
            INSERT INTO glossary_candidate
              (novel_id, source_term, target_term, proposals, first_seen_chapter, last_seen_chapter)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON CONFLICT (novel_id, source_term, target_term) DO UPDATE
            SET proposals = glossary_candidate.proposals
                  + CASE WHEN glossary_candidate.last_seen_chapter = EXCLUDED.last_seen_chapter
                         THEN 0 ELSE 1 END,
                last_seen_chapter = EXCLUDED.last_seen_chapter
            RETURNING proposals
            """,
            (novel_id, source_term, target_term, chapter, chapter),
        )
    ).fetchone()
    return row[0]


async def _lock_glossary(
    db,
    *,
    novel_id: str,
    source_term: str,
    target_term: str,
    entity_id: str,
    chapter: int,
    target_lang: str = "",
    require_corroboration: bool = True,
    min_proposals: int = GLOSSARY_MIN_PROPOSALS,
) -> int | None:
    """Insert one locked term and its tamper-evident audit row.

    The advisory lock makes ``MAX(version)`` and the changelog hash chain a
    per-novel serialized operation. Existing terms are immutable in this stage.

    A blank source or target is refused outright rather than locked. Glossary rows are
    immutable once written and every later translation is validated against them, so a
    single blank row is unrecoverable without hand-editing the table: ``"text".count("")``
    is ``len(text) + 1``, meaning an empty source term silently demands its target appear
    ~2000 times in every chapter and fails all of them, permanently. The LLM proposing an
    empty surface is enough to trigger it, which is exactly how it happened.
    """
    if not source_term.strip() or not target_term.strip():
        raise ValueError(
            f"refusing to lock a blank glossary term: {source_term!r} => {target_term!r}"
        )

    # Guard 2 (migration 0016): shape-check before anything permanent happens. Declining to
    # lock is safe — the surface simply has no locked term, which costs a translation
    # constraint, not correctness. Locking garbage is what is unrecoverable.
    if target_lang:
        problem = _target_term_problem(target_term, target_lang)
        if problem is not None:
            log.warning(
                "resolve: refusing glossary term %r => %r (%s)", source_term, target_term, problem
            )
            return None

    await db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (novel_id,))

    # A human deletion is authoritative until they explicitly add the term again.
    tombstone = await (await db.execute(
        "SELECT 1 FROM glossary WHERE novel_id = %s AND source_term = %s AND deleted",
        (novel_id, source_term),
    )).fetchone()
    if tombstone:
        return None

    # Guard 1 (migration 0016): one target term per novel. A model that returns the same
    # invented name for several distinct entities is the exact failure that made a novel
    # permanently untranslatable — every later translation had to contain that one name
    # once per entity claiming it. First claim wins; later collisions are declined.
    owner = await _target_term_owner(db, novel_id, target_term)
    if owner is not None and owner != source_term:
        log.warning(
            "resolve: refusing glossary term %r => %r (target already locked to %r)",
            source_term,
            target_term,
            owner,
        )
        return None

    # Guard 3 (migration 0016): require corroboration before locking. Skipped for
    # human-supplied terms, where the person is the corroboration.
    if require_corroboration and min_proposals > 1:
        already_locked = await (
            await db.execute(
                "SELECT 1 FROM glossary WHERE novel_id = %s AND source_term = %s",
                (novel_id, source_term),
            )
        ).fetchone()
        if already_locked is None:
            proposals = await _record_candidate(db, novel_id, source_term, target_term, chapter)
            if proposals < min_proposals:
                log.info(
                    "resolve: holding %r => %r provisionally (%d/%d chapters agree)",
                    source_term,
                    target_term,
                    proposals,
                    min_proposals,
                )
                return None

    row = await (
        await db.execute(
            "SELECT version FROM glossary WHERE novel_id = %s ORDER BY version DESC LIMIT 1",
            (novel_id,),
        )
    ).fetchone()
    version = (row[0] if row else 0) + 1
    inserted = await (
        await db.execute(
            """
            INSERT INTO glossary
              (novel_id, source_term, target_term, entity_id, version, locked_at_chapter)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (novel_id, source_term) DO NOTHING
            RETURNING version
            """,
            (novel_id, source_term, target_term, entity_id, version, chapter),
        )
    ).fetchone()
    if inserted is None:
        existing = await (
            await db.execute(
                "SELECT target_term, entity_id, version FROM glossary "
                "WHERE novel_id = %s AND source_term = %s",
                (novel_id, source_term),
            )
        ).fetchone()
        if existing is None:
            raise RuntimeError("glossary conflict reported but the existing row disappeared")
        existing_target, existing_entity, existing_version = existing
        if existing_target != target_term:
            raise ValueError(
                f"glossary term {source_term!r} is already locked to "
                f"{existing_target!r}/{existing_entity}, not {target_term!r}/{entity_id}"
            )
        if existing_entity is None:
            # PLAN.md Phase N6: a glossary-bootstrap term locked before any entity for it
            # existed (locked_at_chapter=0, entity_id NULL). This is the first time RESOLVE
            # has actually created that entity — bind it now rather than raising; a NULL
            # existing_entity is "not yet bound", not a conflicting bind, unlike a real
            # entity_id mismatch (still checked below for the ordinary re-run case).
            await db.execute(
                "UPDATE glossary SET entity_id = %s WHERE novel_id = %s AND source_term = %s",
                (entity_id, novel_id, source_term),
            )
            return existing_version
        if str(existing_entity) != entity_id:
            raise ValueError(
                f"glossary term {source_term!r} is already locked to "
                f"{existing_target!r}/{existing_entity}, not {target_term!r}/{entity_id}"
            )
        return existing_version
    previous = await (
        await db.execute(
            "SELECT seq, row_hash FROM glossary_changelog WHERE novel_id = %s ORDER BY seq DESC LIMIT 1",
            (novel_id,),
        )
    ).fetchone()
    seq = (previous[0] if previous else 0) + 1
    prev_hash = previous[1] if previous else ""
    payload = json.dumps(
        [novel_id, seq, source_term, "", target_term, chapter, prev_hash],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    row_hash = hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()
    await db.execute(
        """
        INSERT INTO glossary_changelog
          (novel_id, seq, source_term, old_target, new_target, changed_at_chapter,
           prev_hash, row_hash)
        VALUES (%s, %s, %s, NULL, %s, %s, %s, %s)
        """,
        (novel_id, seq, source_term, target_term, chapter, prev_hash or None, row_hash),
    )
    # The mapping is locked now, so its provisional records (including any competing
    # targets this source accumulated before settling) have served their purpose.
    await db.execute(
        "DELETE FROM glossary_candidate WHERE novel_id = %s AND source_term = %s",
        (novel_id, source_term),
    )
    return version


async def _locked_target(db, novel_id: str, source_term: str) -> str | None:
    """The glossary's already-locked target_term for source_term, if any (PLAN.md Phase
    N6: a glossary-bootstrap seed locks this before any entity exists for it — see
    _lock_glossary's NULL-entity_id backfill path, which this pairs with)."""
    row = await (
        await db.execute(
            "SELECT target_term FROM glossary WHERE novel_id = %s AND source_term = %s AND NOT deleted",
            (novel_id, source_term),
        )
    ).fetchone()
    return row[0] if row else None


def _context_around(text: str, surface: str, *, window: int = CONTEXT_WINDOW) -> str:
    """The sentence-ish neighbourhood of a surface's first occurrence.

    Disambiguating a bare name is guesswork — "Chen" alone cannot distinguish two
    characters, but "Elder Chen of the Azure Cloud Sect" can. Cheap to include, and it is
    the difference between the model choosing and the model shrugging.
    """
    at = text.find(surface)
    if at < 0:
        return ""
    return text[max(0, at - window) : at + len(surface) + window].strip()


class ResolveStage:
    name = STAGE

    async def run(self, ctx: StageContext, state: PipelineState) -> None:
        envelope = state.envelope
        writer = GraphWriter(ctx.db)
        await writer.ready()

        key = idempotency_key(STAGE, envelope.source_meta.raw_hash, ctx.cfg)
        await insert_job(
            ctx.db,
            novel_id=envelope.novel_id,
            chapter_index=envelope.chapter_index,
            stage=STAGE,
            key=key,
        )

        resolutions: dict[str, str] = {}
        text = envelope.raw_text

        # --- pass 1: scanned exact hits ------------------------------------
        scanned: dict[str, set[str]] = {}
        for span in state.mentions:
            scanned.setdefault(text[span.char_start : span.char_end], set()).add(span.alias_id)

        ambiguous: list[str] = []
        for surface, entity_ids in scanned.items():
            if len(entity_ids) == 1:
                resolutions[surface] = next(iter(entity_ids))
            else:
                # Two entities share this surface. That is not scanner noise — it is a
                # real ambiguity, and choosing between them is exactly this stage's job.
                ambiguous.append(surface)

        # --- pass 2: proposal ----------------------------------------------
        completion = await ctx.provider.complete(
            build_proposal_user_prompt(text),
            system=build_proposal_system_prompt(ctx.novel.ontology),
            json_mode=True,
            cls=Class.BATCH,
            model=model_for_stage(STAGE, ctx.cfg),
        )
        proposal = parse_proposal(completion.text)
        kinds = {m.surface: m.kind for m in proposal.mentions}

        unresolved = ambiguous + [
            m.surface for m in proposal.mentions if m.surface not in resolutions
        ]
        # Drop blank surfaces before they can reach _lock_glossary. The proposal pass is
        # the one generative step here, and a model that returns an empty (or whitespace)
        # surface would otherwise get it locked into the glossary permanently — see
        # _lock_glossary's docstring for why a single blank row bricks translation for the
        # whole novel. Filtering here rather than only raising there keeps one weak
        # proposal from failing the entire chapter.
        unresolved = [surface for surface in unresolved if surface.strip()]
        # dict.fromkeys: dedupe while keeping order, so a re-run resolves in the same
        # sequence and the entities it creates get the same first-seen ordering.
        unresolved = list(dict.fromkeys(unresolved))

        # --- pass 3: disambiguate ------------------------------------------
        # One batched embed call for every surface at once (§5.4: the embed backend takes
        # the whole array). The vector is used twice — to retrieve candidates now, and as
        # the stored embedding if the surface turns out to be a new entity — so a new
        # entity is searchable by the very next chapter.
        vectors = await ctx.embed_provider.embed(unresolved, cls=Class.BATCH) if unresolved else []

        created = 0
        for surface, vector in zip(unresolved, vectors):
            candidates = await self._candidates(writer, ctx.novel.id, surface, vector)
            entity_id, _target_term = await self._decide(
                ctx, writer, surface, candidates, vector, text=text, chapter=envelope.chapter_index,
                kind=kinds.get(surface) or self._kind_of(candidates) or UNKNOWN_KIND,
            )
            if entity_id is None:
                continue
            if not any(c.entity_id == entity_id for c in candidates):
                created += 1
            resolutions[surface] = entity_id

        state.resolutions = resolutions
        # Tracking only: RESOLVE must still run on retries against the live alias
        # index (§3.5). Its writes have completed before this success marker.
        await mark_job_done(ctx.db, key)
        log.info(
            "stage %s chapter=%d scanned=%d proposed=%d resolved=%d created=%d",
            self.name,
            envelope.chapter_index,
            len(scanned),
            len(proposal.mentions),
            len(resolutions),
            created,
        )

    async def _candidates(
        self, writer: GraphWriter, novel_id: str, surface: str, vector: list[float]
    ) -> list[Candidate]:
        """Exact matches first, then nearest neighbours, deduped.

        Exact match is the safety net under the approximate search (0003's note): ANN is
        acceptable here precisely because a candidate it misses is still caught by the
        exact lookup in the same step.
        """
        rows: list[CandidateRow] = await writer.exact_matches(novel_id, surface)
        seen = {r.id for r in rows}
        for row in await writer.similar_entities(novel_id, vector, k=CANDIDATE_K):
            if row.id not in seen:
                seen.add(row.id)
                rows.append(row)
        return [Candidate(entity_id=r.id, canonical=r.canonical, kind=r.kind) for r in rows]

    @staticmethod
    def _kind_of(candidates: list[Candidate]) -> str | None:
        return candidates[0].kind if candidates else None

    async def _decide(
        self,
        ctx: StageContext,
        writer: GraphWriter,
        surface: str,
        candidates: list[Candidate],
        vector: list[float],
        *,
        text: str,
        chapter: int,
        kind: str,
    ) -> tuple[str | None, str | None]:
        """Confirm a candidate or create a new entity. ``None`` means unresolved."""
        locked_target: str | None = None
        if ctx.novel.source_lang != ctx.novel.target_lang:
            locked_target = await _locked_target(ctx.db, ctx.novel.id, surface)

        completion = await ctx.provider.complete(
            build_disambiguation_user_prompt(
                surface, candidates, context=_context_around(text, surface), locked_target=locked_target
            ),
            system=build_disambiguation_system_prompt(
                source_lang=ctx.novel.source_lang,
                target_lang=ctx.novel.target_lang,
            ),
            json_mode=True,
            cls=Class.BATCH,
            model=model_for_stage(STAGE, ctx.cfg),
        )
        try:
            decision = parse_decision(completion.text, candidates)
        except FreeGeneratedEntity:
            # The tripwire fired: the model invented an id. Leaving the surface
            # unresolved drops whatever depended on it, which is the right trade — a
            # fabricated bind corrupts the graph permanently and silently, whereas an
            # unresolved mention is a number the eval set reports.
            log.warning("resolve: free-generated entity id for %r; leaving unresolved", surface)
            return None, None

        if decision.decision != NEW_ENTITY:
            confirmed = next(c for c in candidates if c.entity_id == decision.entity_id)
            alias = AliasRow(
                entity_id=confirmed.entity_id,
                surface=surface,
                lang=ctx.novel.source_lang,
                first_seen_chapter=chapter,
            )
            if ctx.novel.source_lang != ctx.novel.target_lang:
                async with ctx.db.transaction():
                    await writer.upsert_aliases([alias])
                    await _lock_glossary(
                        ctx.db,
                        novel_id=ctx.novel.id,
                        source_term=surface,
                        target_term=confirmed.canonical,
                        entity_id=confirmed.entity_id,
                        chapter=chapter,
                        target_lang=ctx.novel.target_lang,
                        min_proposals=ctx.cfg.glossary_min_proposals,
                    )
            else:
                await writer.upsert_aliases([alias])
            return decision.entity_id, None

        # PLAN.md Phase N6: locked_target (a glossary-bootstrap seed) wins over whatever
        # the model proposed — structural enforcement, not trust in prompt compliance
        # (build_disambiguation_user_prompt's docstring explains why this mirrors
        # FreeGeneratedEntity's "parser enforces, not the prompt" posture).
        target_term = locked_target or decision.target_term
        if ctx.novel.source_lang != ctx.novel.target_lang and not target_term:
            log.warning("resolve: missing target term for translated entity %r", surface)
            return None, None

        entity_id = str(uuid.uuid4())
        entity = EntityRow(
            id=entity_id,
            novel_id=ctx.novel.id,
            kind=kind,
            canonical=(
                target_term
                if ctx.novel.source_lang != ctx.novel.target_lang
                else surface
            ),
            first_seen_chapter=chapter,
            embedding=vector,
        )
        aliases = [
            AliasRow(
                entity_id=entity_id,
                surface=surface,
                lang=ctx.novel.source_lang,
                first_seen_chapter=chapter,
            )
        ]
        if ctx.novel.source_lang != ctx.novel.target_lang:
            async with ctx.db.transaction():
                await writer.insert_entity(entity, aliases)
                await _lock_glossary(
                    ctx.db,
                    novel_id=ctx.novel.id,
                    source_term=surface,
                    target_term=target_term or surface,
                    entity_id=entity_id,
                    chapter=chapter,
                    target_lang=ctx.novel.target_lang,
                    min_proposals=ctx.cfg.glossary_min_proposals,
                )
        else:
            await writer.insert_entity(entity, aliases)
        return entity_id, target_term
