"""The offline ingestion pipeline (instructions.md §5).

Drains the Redis ``jobs:pending`` queue produced by ``ingest-api`` and runs each
chapter through an ordered list of stages. This phase (PLAN.md steps 1.1-1.3) is the
walking skeleton: the worker loop, the provider abstraction, and job/idempotency
bookkeeping exist; the five stages are no-op stubs filled in later steps.
"""
