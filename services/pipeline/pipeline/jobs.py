"""Job-row bookkeeping + idempotency keys (instructions.md §4, §6.1; PLAN.md §1.2).

The idempotency key is a fingerprint of *everything the stage output depends on*:

    sha256(stage ‖ raw_hash ‖ prompt_version ‖ config_version ‖ model_id)

``model_id`` (provider + model) is non-negotiable inside the key: omit it and switching
Ollama→Anthropic would serve the previous backend's cached output under a still-valid key
— silent corruption (§12 risk #4). ``test_idempotency.py`` asserts a backend switch
changes the key.

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


def model_id_for_stage(stage: str, cfg: Config) -> str:
    """provider:model for a stage. translate uses the stronger model; the rest the cheap
    extraction model (§10 / .env.example)."""
    model = cfg.llm_model_translate if stage == "translate" else cfg.llm_model_extract
    return f"{cfg.llm_provider}:{model}"


def idempotency_key(stage: str, raw_hash: str, cfg: Config) -> str:
    """sha256 over everything the stage output depends on (§6.1). Stable for identical
    inputs; changes when the model, prompt_version, or config_version changes."""
    parts = [
        stage,
        raw_hash,
        cfg.prompt_version,
        cfg.config_version,
        model_id_for_stage(stage, cfg),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


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
