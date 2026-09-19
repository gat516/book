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
            "SELECT source_term, id::text FROM subject WHERE novel_id=%s", (novel,))).fetchall())
        rows = await (await db_conn.execute(
            """SELECT category, kind, text, subjects::text[] FROM chapter_fact
                WHERE novel_id=%s AND prompt_version=%s ORDER BY ordinal""", (novel, PROMPT_VERSION))).fetchall()
        assert rows == [
            ("relationship", "superior", f"⟦{ids['温青']}⟧ is ⟦{ids['莫尘']}⟧'s superior.", [ids["温青"], ids["莫尘"]]),
            ("event", None, f"⟦{ids['莫尘']}⟧ left the valley.", [ids["莫尘"]]),
        ]
    finally:
        await delete_novel(db_conn, novel)


KINDS_ANSWER = """## Facts
affiliation | Wen Qing leads the Cloud Gate.
place | The Cloud Gate sits in Frost Valley.
ability | Wen Qing learned Frost Palm.

## Names
organization | Cloud Gate
place | Frost Valley
person | Wen Qing
"""


async def test_listed_organizations_and_places_become_subjects_and_the_rest_stay_text(db_conn):
    novel = await make_novel(db_conn)
    try:
        await db_conn.execute("""INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta)
            VALUES(%s,1,'h','raw/x','{}')""", (novel,))
        for term, spelling, role in (("温青", "Wen Qing", "chinese_person"), ("云门", "Cloud Gate", "semantic_term"),
                                     ("霜谷", "Frost Valley", "semantic_term"), ("霜掌", "Frost Palm", "semantic_term")):
            await db_conn.execute("""INSERT INTO character_name_review
                (novel_id,source_term,first_seen_chapter,source_hash,char_start,char_end,quote,candidates,reason,term_role)
                VALUES(%s,%s,1,'h',0,1,'q',%s,'test',%s)""",
                (novel, term, f'[{{"target_term": "{spelling}", "method": "pinyin"}}]', role))
        ctx = SimpleNamespace(db=db_conn, cfg=make_config(), provider=FakeProvider(KINDS_ANSWER), provider_id="ollama",
                              model_override=None, novel=SimpleNamespace(id=novel, source_lang="zh", target_lang="en"))
        state = SimpleNamespace(translation="text", envelope=SimpleNamespace(
            raw_text="温青云门霜谷霜掌", chapter_index=1, source_meta=SimpleNamespace(raw_hash="h")))
        await FactsStage().run(ctx, state)

        subjects = {term: (sid, kind) for term, sid, kind in await (await db_conn.execute(
            "SELECT source_term, id::text, kind FROM subject WHERE novel_id=%s", (novel,))).fetchall()}
        # The unlisted technique gets no page; "person" is not a Names kind.
        assert {term: kind for term, (_, kind) in subjects.items()} == {
            "温青": "character", "云门": "organization", "霜谷": "place"}
        ids = {term: sid for term, (sid, _) in subjects.items()}
        texts = [row[0] for row in await (await db_conn.execute(
            "SELECT text FROM chapter_fact WHERE novel_id=%s ORDER BY ordinal", (novel,))).fetchall()]
        assert texts == [f"⟦{ids['温青']}⟧ leads the ⟦{ids['云门']}⟧.",
                         f"The ⟦{ids['云门']}⟧ sits in ⟦{ids['霜谷']}⟧.",
                         f"⟦{ids['温青']}⟧ learned Frost Palm."]

        # A later chapter need not list a known subject again: its kind is stored.
        await db_conn.execute("""INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta)
            VALUES(%s,2,'h2','raw/y','{}')""", (novel,))
        ctx.provider = FakeProvider("## Facts\nevent | The Cloud Gate fell.\n\n## Names\n")
        state.envelope = SimpleNamespace(raw_text="云门", chapter_index=2, source_meta=SimpleNamespace(raw_hash="h2"))
        await FactsStage().run(ctx, state)
        [(text,)] = await (await db_conn.execute(
            "SELECT text FROM chapter_fact WHERE novel_id=%s AND chapter_index=2", (novel,))).fetchall()
        assert text == f"The ⟦{ids['云门']}⟧ fell."
    finally:
        await delete_novel(db_conn, novel)
