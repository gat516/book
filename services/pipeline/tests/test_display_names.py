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
    assert await discover_names(ctx, "Ann left.") == []
    assert ctx.cache.redis.store == {}


async def test_one_malformed_name_drops_only_itself():
    ctx = context('{"names":["Ann",{"entity_id":"invented"},"Bo"]}')
    text = "Ann met Bo."
    spans = await discover_names(ctx, text)
    assert [text[s.char_start:s.char_end] for s in spans] == ["Ann", "Bo"]
    assert ctx.cache.redis.store == {}  # partial answers are used once, not cached


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


async def test_alignment_sends_excerpts_in_budgeted_batches_but_checks_the_full_source(monkeypatch):
    import json
    import pipeline.display_names as display_names
    monkeypatch.setattr(display_names, "ALIGN_PAYLOAD_TOKENS", 250)
    names = [f"Name{i}" for i in range(12)]
    # Names spread through a long chapter, so each one needs its own excerpt.
    display = "\n".join(f"{names[i // 20]} arrived." if i % 20 == 10 else f"Filler paragraph {i} with nothing in it."
                        for i in range(240))
    source = "\n".join(f"甲{i // 20}来了。" if i % 20 == 10 else f"填充段落{i}。" for i in range(240))
    reply = {"alignments": [{"display_term": n, "source_term": f"甲{i}"} for i, n in enumerate(names)]
                           + [{"display_term": "Name0", "source_term": "不在原文里"}]}
    ctx = context(json.dumps(reply, ensure_ascii=False))
    spans = []
    for name in names:
        start = display.index(name)
        spans.append(Span(alias_id="", byte_start=start, byte_end=start + len(name),
                          char_start=start, char_end=start + len(name)))
    rows = await align_names(ctx, source, display, spans)
    assert len(ctx.provider.calls) > 1  # split to fit the budget, not one oversized call
    for call in ctx.provider.calls:
        payload = json.loads(call["prompt"].split("\n", 1)[1])
        assert "Filler paragraph 0 " not in payload["translation"]  # excerpts, not the chapter
    # "不在原文里" is absent from the full source, so it cannot displace or add a row.
    assert {(r.display_term, r.source_term) for r in rows} == {(n, f"甲{i}") for i, n in enumerate(names)}


async def test_alignment_classifies_term_in_the_existing_call_without_spelling_suggestions():
    ctx = context('{"alignments":[{"display_term":"Lingfeng","source_term":"凌峰","term_role":"chinese_person"}]}')
    rows = await align_names(ctx, "凌峰来了。", "Lingfeng came.", [
        Span(alias_id="", byte_start=0, byte_end=8, char_start=0, char_end=8)])
    assert rows[0].term_role == "chinese_person"
    assert len(ctx.provider.calls) == 1
    assert "targets" not in ctx.provider.calls[0]["json_schema"]["properties"]
