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

import logging
import uuid
import json
import hashlib

from pipeline.context import PipelineState, StageContext
from pipeline.graph import AliasRow, CandidateRow, EntityRow, GraphWriter
from pipeline.jobs import idempotency_key, insert_job, model_for_stage
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


async def _lock_glossary(
    db,
    *,
    novel_id: str,
    source_term: str,
    target_term: str,
    entity_id: str,
    chapter: int,
) -> int:
    """Insert one locked term and its tamper-evident audit row.

    The advisory lock makes ``MAX(version)`` and the changelog hash chain a
    per-novel serialized operation. Existing terms are immutable in this stage.
    """
    await db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (novel_id,))
    row = await (
        await db.execute(
            "SELECT version FROM glossary WHERE novel_id = %s ORDER BY version DESC LIMIT 1",
            (novel_id,),
        )
    ).fetchone()
    version = (row[0] if row else 0) + 1
    await db.execute(
        """
        INSERT INTO glossary (novel_id, source_term, target_term, entity_id, version, locked_at_chapter)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (novel_id, source_term) DO NOTHING
        """,
        (novel_id, source_term, target_term, entity_id, version, chapter),
    )
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
    return version


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

        await insert_job(
            ctx.db,
            novel_id=envelope.novel_id,
            chapter_index=envelope.chapter_index,
            stage=STAGE,
            key=idempotency_key(STAGE, envelope.source_meta.raw_hash, ctx.cfg),
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
        completion = await ctx.provider.complete(
            build_disambiguation_user_prompt(
                surface, candidates, context=_context_around(text, surface)
            ),
            system=build_disambiguation_system_prompt(),
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
            return decision.entity_id, None

        if ctx.novel.source_lang != ctx.novel.target_lang and not decision.target_term:
            log.warning("resolve: missing target term for translated entity %r", surface)
            return None, None

        entity_id = str(uuid.uuid4())
        entity = EntityRow(
            id=entity_id,
            novel_id=ctx.novel.id,
            kind=kind,
            canonical=decision.target_term or surface,
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
                    target_term=decision.target_term or surface,
                    entity_id=entity_id,
                    chapter=chapter,
                )
        else:
            await writer.insert_entity(entity, aliases)
        return entity_id, decision.target_term
