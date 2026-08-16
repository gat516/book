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
uses ``glossary_version``, state uses ``ontology_version``. Neither exists yet (both are
introduced when their stages go from stub to real, PLAN.md §1.6/§1.7/§1.5), so
``stage_config_version`` currently falls back to ``cfg.config_version`` for every stage —
this function is the single place that changes when they land, not every call site.

Note (§4, PLAN.md §1.2): the ``resolve`` stage is deliberately NOT content-cached — its
output depends on the live alias index (DB state), not just chapter text. Only
``translate`` and ``state`` are pure functions of (content, prompt, config, model) and
therefore safe to skip on a cache hit. This module computes keys for all LLM-bearing
stages for *tracking*; the cache-skip logic (step 1.5) applies only to translate/state.
"""

from __future__ import annotations

import hashlib

from pipeline.config import Config

# LLM-bearing stages that get a job row (the job.stage enum, 0001_init.sql:130).
# chunk/scan are not here: chunk is pure CPU (no LLM, no row); scan's LLM work is folded
# into resolve.
LLM_STAGES = ("extract", "resolve", "translate", "state")

_UNIT_SEPARATOR = "\x1f"


def model_for_stage(stage: str, cfg: Config) -> str:
    """The bare model name (no provider prefix) a stage should ask the provider for.

    THE single source of truth for stage→model selection. Stage code MUST call this
    and pass the result as ``LLMProvider.complete(..., model=...)`` rather than relying
    on whatever model the provider instance happened to be constructed with — the two
    used to disagree silently (llm/__init__.py used to build one instance pinned to
    ``llm_model_extract`` and hand it to every stage, including translate, while this
    module computed the translate key assuming ``llm_model_translate`` was actually
    used). Routing every stage's model choice through this one function is what makes
    that class of bug structurally impossible instead of merely avoided by convention.
    """
    return cfg.llm_model_translate if stage == "translate" else cfg.llm_model_extract


def stage_config_version(stage: str, cfg: Config) -> str:
    """Per-stage config version input to the idempotency key (§3.5).

    Resolves to ``cfg.config_version`` today because ``glossary_version`` (translate)
    and ``ontology_version`` (state) don't exist until their stages are real. When they
    land, branch here — not at every call site.
    """
    return cfg.config_version


def model_id_for_stage(stage: str, cfg: Config) -> str:
    """provider:model for a stage. translate uses the stronger model; the rest the cheap
    extraction model (§10 / .env.example)."""
    return f"{cfg.llm_provider}:{model_for_stage(stage, cfg)}"


def idempotency_key(stage: str, raw_hash: str, cfg: Config) -> str:
    """sha256 over everything the stage output depends on (§3.5, §6.1). Stable for
    identical inputs; changes when the model, prompt_version, or stage config changes."""
    parts = [
        stage,
        raw_hash,
        cfg.prompt_version,
        stage_config_version(stage, cfg),
        model_id_for_stage(stage, cfg),
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
    """Insert a job row, idempotent on the UNIQUE(idempotency_key) constraint
    (0001_init.sql:136). Re-processing the same input is a no-op on the row."""
    await conn.execute(
        """
        INSERT INTO job (novel_id, chapter_index, stage, idempotency_key)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (novel_id, chapter_index, stage, key),
    )
