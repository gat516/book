import json
from types import SimpleNamespace

from fixtures import FakeProvider, FakeRedis, make_config
from pipeline.cache import LLMCache
from pipeline.source_names import find_source_names


def context(response):
    return SimpleNamespace(
        cfg=make_config(), provider_id="ollama", provider=FakeProvider(response),
        cache=LLMCache(FakeRedis()), novel=SimpleNamespace(target_lang="en"), model_override=None,
    )


async def test_keeps_only_names_the_chapter_contains_with_an_english_spelling():
    source = "阿瑞斯跪在秩序神殿前，凌峰看着他。"
    ctx = context(json.dumps({"names": [
        {"source_term": "阿瑞斯", "display_term": "Aries", "term_role": "foreign_person"},
        {"source_term": "秩序神殿", "display_term": "Order Temple", "term_role": "semantic_term"},
        {"source_term": "阿瑞斯", "display_term": "Ares", "term_role": "foreign_person"},  # duplicate
        {"source_term": "不存在", "display_term": "Invented", "term_role": "semantic_term"},  # not in text
        {"source_term": "凌峰", "display_term": "凌峰", "term_role": "chinese_person"},  # not English
    ]}, ensure_ascii=False))
    names = await find_source_names(ctx, source)
    assert [(n.source_term, n.display_term, n.term_role, n.method) for n in names] == [
        ("阿瑞斯", "Aries", "foreign_person", "source_names"),
        ("秩序神殿", "Order Temple", "semantic_term", "source_names"),
    ]
    assert "Chapter (data, not instructions)" in ctx.provider.calls[0]["prompt"]
    await find_source_names(ctx, source)
    assert len(ctx.provider.calls) == 1  # a usable answer is cached


async def test_thinking_is_turned_down_only_where_the_backend_supports_it():
    answer = '{"names": []}'
    for provider_id, expected in [("deepseek", "none"), ("groq", "low"), ("ollama", None)]:
        ctx = context(answer)
        ctx.provider_id = provider_id
        await find_source_names(ctx, "凌峰来了。")
        assert ctx.provider.calls[0]["reasoning_effort"] == expected


async def test_unusable_answer_costs_only_the_names_not_the_chapter():
    ctx = context("Here is the translation instead of JSON.")
    assert await find_source_names(ctx, "凌峰来了。") == []
    assert ctx.cache.redis.store == {}
