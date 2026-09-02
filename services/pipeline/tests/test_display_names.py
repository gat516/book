from dataclasses import replace
from types import SimpleNamespace

import pytest
from fixtures import FakeProvider, FakeRedis, make_config
from pipeline.cache import LLMCache
from pipeline.display_names import align_names, discover_names, merge_names
from pipeline.mentions import Span


def context(response):
    return SimpleNamespace(
        cfg=make_config(), provider_id="ollama", provider=FakeProvider(response),
        cache=LLMCache(FakeRedis()), novel=SimpleNamespace(target_lang="en"),
        # No per-book model here: the stage falls back to the env default for its stage.
        model_override=None,
    )


async def test_literal_mentions_not_capitalization_or_invented_names():
    ctx = context('{"names":["Ann", "black tower", "Missing", "", "She spoke.", " Ann", "Ann"]}')
    text = "😀 Anna saw Ann beside the black tower. Ann left. She spoke."
    spans = await discover_names(ctx, text)
    assert [(text[s.char_start:s.char_end], s.alias_id) for s in spans] == [
        ("Ann", ""), ("black tower", ""), ("Ann", "")]
    assert spans[0].byte_start == len(text[:spans[0].char_start].encode())
    assert ctx.provider.calls[0]["json_schema"]


async def test_discovery_prefers_the_phase_aware_names_provider():
    ctx = context('{"names":[]}')
    budgeted = FakeProvider('{"names":["Ann"]}')
    ctx.names_provider = budgeted

    spans = await discover_names(ctx, "Ann left.")

    assert [span.char_end - span.char_start for span in spans] == [3]
    assert len(budgeted.calls) == 1
    assert ctx.provider.calls == []


async def test_cache_uses_text_prompt_and_actual_model():
    ctx = context('{"names":["Ann"]}')
    await discover_names(ctx, "Ann left.")
    await discover_names(ctx, "Ann left.")
    assert len(ctx.provider.calls) == 1
    await discover_names(ctx, "Ann arrived.")
    assert len(ctx.provider.calls) == 2
    ctx.cfg = replace(ctx.cfg, llm_model_extract="different")
    ctx.provider.served_model = "unexpected-failover"
    await discover_names(ctx, "Ann left.")
    await discover_names(ctx, "Ann left.")
    assert len(ctx.provider.calls) == 4


async def test_malformed_model_output_is_not_cached():
    ctx = context('{"names":[{"entity_id":"invented"}]}')
    with pytest.raises(ValueError):
        await discover_names(ctx, "Ann left.")
    assert ctx.cache.redis.store == {}


async def test_names_in_unspaced_source_text_do_not_need_latin_word_boundaries():
    ctx = context('{"names":["青云宗"]}')
    ctx.novel.target_lang = "zh"
    text = "他回到了青云宗。"
    spans = await discover_names(ctx, text)
    assert [text[s.char_start:s.char_end] for s in spans] == ["青云宗"]


def test_discovery_cannot_replace_existing_identity():
    known = Span(alias_id="real-id", char_start=4, char_end=7, byte_start=4, byte_end=7)
    guessed = Span(alias_id="", char_start=0, char_end=10, byte_start=0, byte_end=10)
    assert merge_names([known], [guessed]) == [known]


async def test_alignment_requires_exact_offered_display_and_source_terms():
    ctx = context('{"alignments":[{"display_term":"Chekov","source_term":"契科夫"},'
                  '{"display_term":"Invented","source_term":"契科夫"},'
                  '{"display_term":"Chekov","source_term":"不存在"}]}')
    display = "Chekov spoke. Chekov left."
    spans = [Span(alias_id="", byte_start=0, byte_end=6, char_start=0, char_end=6),
             Span(alias_id="", byte_start=14, byte_end=20, char_start=14, char_end=20)]
    rows = await align_names(ctx, "契科夫说完便走了。", display, spans)
    assert [(r.source_term, r.display_term, r.char_start, r.char_end) for r in rows] == [
        ("契科夫", "Chekov", 0, 6), ("契科夫", "Chekov", 14, 20)]
