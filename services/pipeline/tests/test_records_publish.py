from types import SimpleNamespace

import pytest

from pipeline.records_publish import _display_binding_id, _embedding_rows


def _ctx(provider, dim=3):
    return SimpleNamespace(embed_provider=provider, cfg=SimpleNamespace(embed_dim=dim))


def test_display_binding_requires_exact_surface_resolution():
    first = "00000000-0000-0000-0000-000000000001"
    second = "00000000-0000-0000-0000-000000000002"
    entity_ids = {"e1": first, "e2": second}
    span = SimpleNamespace(char_start=0, char_end=4)
    rendering = SimpleNamespace(source_term="甲", display_term="Anna", char_start=0, char_end=4)
    assert _display_binding_id(first, "Anna", span, [rendering], {"甲": "e1"}, entity_ids) == first
    # A UUID belonging to a different resolved entity must not be accepted merely
    # because it appears somewhere in the current resolution.
    assert _display_binding_id(second, "Anna", span, [rendering], {"甲": "e1"}, entity_ids) is None
    # Glossary-only display terms absent from the authoritative resolution remain NULL.
    assert _display_binding_id(first, "Anna", span, [rendering], {}, entity_ids) is None
    assert _display_binding_id("e1", "Anna", span, [rendering], {"甲": "e1"}, entity_ids) is None


@pytest.mark.asyncio
async def test_embedding_failure_keeps_publication_usable_without_vectors():
    class Unavailable:
        async def embed(self, texts):
            raise OSError("embedding service unavailable")

    chunks = [SimpleNamespace(text="first"), SimpleNamespace(text="second")]
    assert await _embedding_rows(_ctx(Unavailable()), chunks) == [None, None]


@pytest.mark.asyncio
async def test_embedding_width_drift_is_retrieval_only():
    class WrongWidth:
        async def embed(self, texts):
            return [[0.0, 1.0]] * len(texts)

    chunks = [SimpleNamespace(text="first")]
    assert await _embedding_rows(_ctx(WrongWidth()), chunks) == [None]


@pytest.mark.asyncio
async def test_valid_embeddings_are_preserved():
    class Valid:
        async def embed(self, texts):
            return [[0.0, 1.0, 2.0] for _ in texts]

    chunks = [SimpleNamespace(text="first"), SimpleNamespace(text="second")]
    assert await _embedding_rows(_ctx(Valid()), chunks) == [[0.0, 1.0, 2.0], [0.0, 1.0, 2.0]]


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_fact_first_publication_persists_optional_vector_and_space(db_conn, monkeypatch, available):
    import uuid
    from unittest.mock import AsyncMock
    import pipeline.records_publish as publisher

    class Embeddings:
        async def embed(self, texts):
            if not available:
                raise OSError("hosted embedding API unavailable")
            return [[1.] + [0.] * 767 for _ in texts]

    # Exercise the real insert/pgvector adaptation and publication transaction, while
    # the independent generation-fence tests own extraction identity validation.
    monkeypatch.setattr(publisher, "verify_generation", AsyncMock())
    async with db_conn.transaction(force_rollback=True):
        novel = str(uuid.uuid4())
        await db_conn.execute(
            "INSERT INTO novel(id,title,source_lang,target_lang,ontology) VALUES (%s,'Embedding publication','en','en','{}')",
            (novel,))
        generation = (await (await db_conn.execute(
            "SELECT active_record_generation FROM novel WHERE id=%s", (novel,))).fetchone())[0]
        await db_conn.execute("INSERT INTO chapter(novel_id,chapter_index,raw_uri,raw_hash,source_meta) VALUES (%s,1,'raw','hash','{}')", (novel,))
        run = (await (await db_conn.execute(
            "INSERT INTO fact_first_run(novel_id,generation_id,chapter_index,source_hash,request_identity,baseline_commit) VALUES (%s,%s,1,'hash','test','test') RETURNING id",
            (novel, generation))).fetchone())[0]
        ctx = SimpleNamespace(db=db_conn, novel=SimpleNamespace(id=novel, source_lang="en"),
                              cfg=SimpleNamespace(embed_dim=768), embed_provider=Embeddings(),
                              embedding_space="hosted-test-space")
        state = SimpleNamespace(envelope=SimpleNamespace(chapter_index=1, raw_text="Visible text",
                                source_meta=SimpleNamespace(raw_hash="hash")),
                                translation="Visible text", record_generation_id=str(generation),
                                chunks=[SimpleNamespace(text="Visible text")])
        await publisher.publish_fact_first(ctx, state, {"fact_first_run_id": str(run)})
        row = await (await db_conn.execute(
            "SELECT text,embedding IS NOT NULL,embedding_space FROM chunk WHERE novel_id=%s", (novel,))).fetchone()
        assert row == ("Visible text", available, "hosted-test-space" if available else None)
        assert (await (await db_conn.execute("SELECT status FROM fact_first_run WHERE id=%s", (run,))).fetchone())[0] == "published"
