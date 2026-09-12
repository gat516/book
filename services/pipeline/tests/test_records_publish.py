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
