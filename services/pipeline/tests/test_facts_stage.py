from types import SimpleNamespace

import pytest

from fixtures import FakeProvider, delete_novel, make_config, make_novel
from pipeline.stages.facts import PROMPT_VERSION, FactsStage

pytestmark = pytest.mark.db

ANSWER = """## Facts
relationship: superior | Wen Qing is Mo Chen's superior.
event | Mo Chen left the valley.
weather | ignored line
"""


async def test_facts_are_tagged_and_name_characters_by_id(db_conn):
    novel = await make_novel(db_conn)
    try:
        await db_conn.execute("""INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta)
            VALUES(%s,1,'h','raw/x','{}')""", (novel,))
        for term, spelling in (("温青", "Wen Qing"), ("莫尘", "Mo Chen")):
            await db_conn.execute("""INSERT INTO character_name_review
                (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason,term_role)
                VALUES(%s,%s,1,'h',0,1,'q',%s,'test','chinese_person')""",
                (novel, term, f'[{{"target_term": "{spelling}", "method": "pinyin"}}]'))
        ctx = SimpleNamespace(db=db_conn, cfg=make_config(), provider=FakeProvider(ANSWER), provider_id="ollama",
                              model_override=None, novel=SimpleNamespace(id=novel, source_lang="zh", target_lang="en"))
        state = SimpleNamespace(translation="Wen Qing and Mo Chen.", envelope=SimpleNamespace(
            raw_text="温青和莫尘。", chapter_index=1, source_meta=SimpleNamespace(raw_hash="h")))
        await FactsStage().run(ctx, state)

        ids = dict(await (await db_conn.execute(
            "SELECT source_term, id::text FROM character WHERE novel_id=%s", (novel,))).fetchall())
        rows = await (await db_conn.execute(
            """SELECT category, kind, text, subjects::text[] FROM chapter_fact
                WHERE novel_id=%s AND prompt_version=%s ORDER BY ordinal""", (novel, PROMPT_VERSION))).fetchall()
        assert rows == [
            ("relationship", "superior", f"⟦{ids['温青']}⟧ is ⟦{ids['莫尘']}⟧'s superior.", [ids["温青"], ids["莫尘"]]),
            ("event", None, f"⟦{ids['莫尘']}⟧ left the valley.", [ids["莫尘"]]),
        ]
    finally:
        await delete_novel(db_conn, novel)
