"""Job-row bookkeeping + idempotency keys (instructions.md §4, §6.1, §3.5; PLAN.md §1.2).

The idempotency key is a fingerprint of *everything the stage output depends on*:

    sha256(stage \x1f raw_hash \x1f prompt_version \x1f stage_config_version \x1f model_id)

joined by the ASCII unit separator (0x1F), per spec §3.5 — not a printable delimiter like
``|``, which can legally appear inside a model id and make the join ambiguous in
principle.

``model_id`` (provider + model) is non-negotiable inside the key: omit it and switching
Ollama→Anthropic would serve the previous backend's cached output under a still-valid key
— silent corruption (§12 risk #4). ``test_idempotency.py`` asserts a backend switch
changes the key.

``stage_config_version`` resolves PER STAGE (§3.5), not to one global value: translate
uses ``glossary_version``, state uses ``ontology_version``. The translate path retains
a config-version fallback for callers that compute a key before loading a glossary.

Note (§4, PLAN.md §1.2): the ``resolve`` stage is deliberately NOT content-cached — its
output depends on the live alias index (DB state), not just chapter text. Only
``translate`` and ``state`` cache model output. ``character_names`` may resume from its
durable done marker because all of its effects are already committed database rows and it
contributes no transient PipelineState; it does not reuse raw model output. This module
computes keys for all LLM-bearing stages for tracking and resume boundaries.
"""

from __future__ import annotations

import hashlib
import json

from pipeline.config import Config

# LLM-bearing stages that get a job row (the job.stage enum, 0001_init.sql:130).
# chunk/scan are not here: chunk is pure CPU (no LLM, no row); scan's LLM work is folded
# into resolve.
LLM_STAGES = ("extract", "character_names", "resolve", "translate", "state")

_UNIT_SEPARATOR = "\x1f"


def model_for_stage(stage: str, cfg: Config, override: str | None = None) -> str:
    """The bare model name (no provider prefix) a stage should ask the provider for.

    THE single source of truth for stage→model selection. Stage code MUST call this
    and pass the result as ``LLMProvider.complete(..., model=...)`` rather than relying
    on whatever model the provider instance happened to be constructed with — the two
    used to disagree silently (llm/__init__.py used to build one instance pinned to
    ``llm_model_extract`` and hand it to every stage, including translate, while this
    module computed the translate key assuming ``llm_model_translate`` was actually
    used). Routing every stage's model choice through this one function is what makes
    that class of bug structurally impossible instead of merely avoided by convention.

    ``override`` is the novel's own configured model (novel_provider_config.model). It
    wins for every stage: a book picks one model, not one per stage. Without this the
    per-book model was silently ignored -- stages pass model= explicitly, so the model on
    the provider instance never applied, and a novel pinned to Gemini was still sent the
    env's Ollama model name (a 404 on every call).
    """
    if override:
        return override
    return cfg.llm_model_translate if stage == "translate" else cfg.llm_model_extract


def ontology_version(ontology: dict) -> str:
    """The state stage's ``stage_config_version`` (§3.5): a content hash of the ontology.

    Why a hash instead of an ``ontology_version`` column on ``novel``: the state prompt is
    *templated on the ontology* (§4.1, extraction.py), so the ontology is a genuine input
    to the stage's output and must be in the cache key. A column would be a second thing
    to remember to bump — and the failure mode of forgetting is silent, since a stale key
    serves extractions shaped by the *previous* ontology while the prompt asks for the new
    one. Hashing the value that actually reaches the prompt makes them impossible to
    disagree, at the cost of one sha256 per chapter and a key that changes on cosmetic
    edits (reordering a kinds list re-extracts the novel). That trade is right at this
    scale and is the sort of thing to revisit only if ontology edits become routine.

    Serialization is canonical (sorted keys, no incidental whitespace) so that two
    equal-but-differently-serialized ontologies hash the same — otherwise a round trip
    through Postgres JSONB could silently invalidate every key.
    """
    canonical = json.dumps(ontology, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def stage_config_version(
    stage: str,
    cfg: Config,
    *,
    ontology: dict | None = None,
    glossary_version: int | None = None,
) -> str:
    """Per-stage config version input to the idempotency key (§3.5).

    ``state`` resolves to the ontology hash and REQUIRES ``ontology`` — passing none
    raises rather than quietly falling back to ``cfg.config_version``, because that
    fallback is indistinguishable from a correct key until the day someone edits an
    ontology and every chapter serves a stale extraction. Translate uses the supplied
    glossary snapshot version and otherwise falls back for compatibility.
    """
    if stage == "state":
        if ontology is None:
            raise ValueError(
                "the state stage's config version is the ontology hash (§3.5); "
                "pass ontology= so an ontology edit invalidates the cache"
            )
        return ontology_version(ontology)
    if stage == "translate" and glossary_version is not None:
        return str(glossary_version)
    return cfg.config_version


def model_id_for_stage(stage: str, cfg: Config, provider: str | None = None,
                      override: str | None = None) -> str:
    """provider:model for a stage. translate uses the stronger model; the rest the cheap
    extraction model (§10 / .env.example).

    ``provider``/``override`` carry the novel's own resolved provider and model, so two
    novels on different backends cannot share a cache key. cacheable_result already stops
    a mismatched result being WRITTEN under the wrong key, but the read side needs this:
    without it a Gemini novel could read an entry an Ollama novel had produced for the
    same raw_hash.
    """
    return f"{provider or cfg.llm_provider}:{model_for_stage(stage, cfg, override)}"


def idempotency_key(
    stage: str,
    raw_hash: str,
    cfg: Config,
    *,
    ontology: dict | None = None,
    glossary_version: int | None = None,
    model_id: str | None = None,
) -> str:
    """sha256 over everything the stage output depends on (§3.5, §6.1). Stable for
    identical inputs; changes when the model, prompt_version, or stage config changes.

    ``ontology`` is required for the ``state`` stage and ignored by the others.
    ``model_id`` overrides configuration when a translation novel is pinned to the
    concrete provider snapshot that actually served its first chapter.
    """
    parts = [
        stage,
        raw_hash,
        cfg.prompt_version,
        stage_config_version(
            stage, cfg, ontology=ontology, glossary_version=glossary_version
        ),
        model_id or model_id_for_stage(stage, cfg),
    ]
    return hashlib.sha256(_UNIT_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


def cacheable_result(stage: str, *, requested_model_id: str, served_provider: str, served_model: str) -> bool:
    """Whether an LLM result may be written under this stage's cache key (§12, §14.3).

    A provider (today; a gateway later, §14) that reports a ``served_model`` different
    from what was requested must NOT have its output cached under the requested key —
    that is precisely how a failover silently serves model B's output as if model A
    produced it. Skipping the cache write on mismatch is the deliberately simple fix:
    no dual-keying scheme, just "don't cache what you can't attribute."
    """
    served_id = f"{served_provider}:{served_model}"
    return served_id == requested_model_id


async def insert_job(
    conn,
    *,
    novel_id: str,
    chapter_index: int,
    stage: str,
    key: str,
) -> None:
    """Insert a chapter-owned job row idempotently.

    The response fingerprint may intentionally be shared by equal inputs, but durable
    completion may not: writing one novel's graph never completes another novel's
    chapter (migration 0029).
    """
    await conn.execute(
        """
        INSERT INTO job (novel_id, chapter_index, stage, idempotency_key)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (novel_id, chapter_index, stage, idempotency_key) DO NOTHING
        """,
        (novel_id, chapter_index, stage, key),
    )


async def job_is_done(
    conn, *, novel_id: str, chapter_index: int, stage: str, key: str
) -> bool:
    """Has this exact work already been written to the graph?

    The durable half of the §6.1 cache, and the one that protects correctness rather
    than cost. ``fact``/``edge``/``event`` are append-only (§0.2) — INSERT, never upsert
    — so a chapter re-run whose extraction was already written would duplicate every row
    it produced. The Redis result cache cannot answer this: it says "we know what the
    model said", not "we already stored it". See cache.py.
    """
    row = await (
        await conn.execute(
            "SELECT state FROM job WHERE novel_id=%s AND chapter_index=%s "
            "AND stage=%s AND idempotency_key=%s",
            (novel_id, chapter_index, stage, key),
        )
    ).fetchone()
    return row is not None and row[0] == "done"


async def mark_job_done(
    conn, *, novel_id: str, chapter_index: int, stage: str, key: str
) -> None:
    """Flip a job to ``done``. Call this INSIDE the transaction that writes the graph
    rows, never after it — a crash in the gap would leave the rows written and the job
    still ``pending``, and the retry would duplicate them (the exact failure ``job_is_done``
    exists to prevent)."""
    await conn.execute(
        "UPDATE job SET state = 'done', updated_at = now() "
        "WHERE novel_id=%s AND chapter_index=%s AND stage=%s AND idempotency_key=%s",
        (novel_id, chapter_index, stage, key),
    )
