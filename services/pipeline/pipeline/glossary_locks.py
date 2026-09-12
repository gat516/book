"""Glossary locking and term validation, shared by the stages that own terminology.

Lifted verbatim out of the retired RESOLVE stage (§0.2): locking a source->target term
is terminology work, not identity work. Identity now belongs to the records pipeline's
who's-who pass, which never writes glossary rows. Keeping these helpers in their own
module lets CHARACTER-NAMES (and the glossary correction path in ingest-api's Go port)
depend on terminology alone.

Forward-only, as before: a term locked while enriching chapter N applies to chapter N+1
onward and never rewrites prose already published (§0.2).
"""
from __future__ import annotations

import logging
import re

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
MAX_SOURCE_TERM_CHARS = 80

# A source term is SUBSTITUTED INTO the chapter before translation
# (translation.prime_glossary_terms), and CJK has no word delimiters, so a one-character
# term rewrites every compound that merely contains it: locking 神 turns 精神 into "精God".
# Two characters is the shortest surface that is a name rather than a morpheme.
MIN_SOURCE_TERM_CHARS = 2

# Sentence punctuation (or a line break) means the model handed back prose, not a surface.
# Observed for real: alias rows in this repo's own corpus hold whole sentences.
_PROSE_MARKERS = re.compile(r"[\n\r。！？；：，、]")

_CJK_RANGE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_CJK_LANGS = frozenset({"zh", "ja"})


class _OccurrencesDisagree(Exception):
    """Raised to roll back every entity minted for a surface whose occurrences disagree."""



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


def _source_term_problem(source_term: str) -> str | None:
    """Return why this source term is unusable as a locked surface, or None if it looks
    like a name.

    The target-side twin of ``_target_term_problem``, and load-bearing for the same reason:
    a locked term is immutable and is now substituted directly into every later chapter
    before the model sees it, so garbage here corrupts the text being translated rather
    than merely costing a constraint. Declining to lock stays the safe outcome.
    """
    source = source_term.strip()
    if len(source) < MIN_SOURCE_TERM_CHARS:
        return f"too short to substitute safely ({len(source)} chars)"
    if len(source) > MAX_SOURCE_TERM_CHARS:
        return f"looks like prose, not a name ({len(source)} chars)"
    if _PROSE_MARKERS.search(source):
        return "contains sentence punctuation, so it is prose rather than a name"
    return None


async def _target_term_owner(db, novel_id: str, target_term: str) -> str | None:
    """Which source term already claims this target in this novel, if any."""
    row = await (
        await db.execute(
            "SELECT source_term FROM glossary WHERE novel_id = %s AND target_term = %s "
            "AND NOT deleted AND constraint_class='semantic_term'",
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
    await db.execute("""INSERT INTO glossary_candidate_chapter VALUES(%s,%s,%s,%s)
        ON CONFLICT DO NOTHING""",(novel_id,source_term,target_term,chapter))
    row = await (await db.execute("""
        INSERT INTO glossary_candidate(novel_id,source_term,target_term,proposals,first_seen_chapter,last_seen_chapter)
        SELECT %s,%s,%s,count(*),min(chapter_index),max(chapter_index)
        FROM glossary_candidate_chapter WHERE novel_id=%s AND source_term=%s AND target_term=%s
        ON CONFLICT(novel_id,source_term,target_term) DO UPDATE SET
        proposals=EXCLUDED.proposals,last_seen_chapter=EXCLUDED.last_seen_chapter RETURNING proposals
        """,(novel_id,source_term,target_term,novel_id,source_term,target_term))).fetchone()
    return row[0]


async def _lock_glossary(
    db,
    *,
    novel_id: str,
    source_term: str,
    target_term: str,
    entity_id: str | None,
    chapter: int,
    target_lang: str = "",
    require_corroboration: bool = True,
    min_proposals: int = GLOSSARY_MIN_PROPOSALS,
    constraint_class: str = "semantic_term",
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
    problem = _source_term_problem(source_term)
    if problem is not None:
        log.warning(
            "resolve: refusing glossary term %r => %r (source: %s)",
            source_term, target_term, problem,
        )
        return None
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

    existing = await (await db.execute(
        "SELECT target_term,entity_id,version,constraint_class FROM glossary "
        "WHERE novel_id=%s AND source_term=%s AND NOT deleted",
        (novel_id,source_term))).fetchone()
    if existing:
        existing_target,existing_entity,existing_version,existing_class=existing
        if existing_target!=target_term:
            log.warning("resolve: preserving existing glossary wording %r => %r; automatic proposal %r remains unapplied",
                        source_term,existing_target,target_term)
            return None
        if existing_class != constraint_class:
            log.warning("resolve: preserving existing glossary class %r for %r",existing_class,source_term)
            return None
        if entity_id is not None and existing_entity is None:
            await db.execute("UPDATE glossary SET entity_id=%s WHERE novel_id=%s AND source_term=%s",
                             (entity_id,novel_id,source_term))
        elif entity_id is not None and str(existing_entity)!=entity_id:
            log.warning("resolve: preserving existing entity binding for glossary term %r",source_term)
            return None
        return existing_version

    # Guard 1 (migration 0016): one target term per novel. A model that returns the same
    # invented name for several distinct entities is the exact failure that made a novel
    # permanently untranslatable — every later translation had to contain that one name
    # once per entity claiming it. First claim wins; later collisions are declined.
    owner = await _target_term_owner(db, novel_id, target_term) if constraint_class == "semantic_term" else None
    if owner is not None and owner != source_term:
        if require_corroboration:
            await _record_candidate(db,novel_id,source_term,target_term,chapter)
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
              (novel_id, source_term, target_term, entity_id, version, locked_at_chapter,
               constraint_class)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (novel_id, source_term) DO NOTHING
            RETURNING version
            """,
            (novel_id, source_term, target_term, entity_id, version, chapter, constraint_class),
        )
    ).fetchone()
    if inserted is None:
        existing = await (await db.execute(
            "SELECT target_term, entity_id, version FROM glossary WHERE novel_id=%s AND source_term=%s",
            (novel_id,source_term))).fetchone()
        if existing is None:
            raise RuntimeError("glossary conflict reported but the existing row disappeared")
        existing_target, existing_entity, existing_version = existing
        if existing_target != target_term:
            return None
        if entity_id is None:
            # Managed graph identity is revision-scoped. Global wording stays detached
            # and glossary_binding carries the current revision's entity.
            return existing_version
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
            return None
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


def _contexts_around(text: str, surface: str, *, window: int = CONTEXT_WINDOW) -> list[str]:
    return [text[max(0,m.start()-window):min(len(text),m.end()+window)].strip()
            for m in re.finditer(re.escape(surface),text)]


def _valid_surface(text: str, surface: str, lang: str) -> bool:
    if not surface or surface != surface.strip() or surface not in text:
        return False
    if lang.split("-")[0] in {"zh","ja","ko"}:
        return True
    return any(not ((m.start()>0 and (text[m.start()-1].isalnum() or text[m.start()-1]=="_"))
                    or (m.end()<len(text) and (text[m.end()].isalnum() or text[m.end()]=="_")))
               for m in re.finditer(re.escape(surface),text))

