import pytest

from fixtures import delete_novel, make_novel
from pipeline.jobs import insert_job,job_is_done,mark_job_done

pytestmark=pytest.mark.db


async def test_identical_work_is_durable_per_novel_and_chapter(db_conn):
    first=await make_novel(db_conn);second=await make_novel(db_conn)
    try:
        for novel in (first,second):
            await insert_job(db_conn,novel_id=novel,chapter_index=1,stage="translate",key="same-content")
        await mark_job_done(db_conn,novel_id=first,chapter_index=1,stage="translate",key="same-content")
        assert await job_is_done(db_conn,novel_id=first,chapter_index=1,stage="translate",key="same-content")
        assert not await job_is_done(db_conn,novel_id=second,chapter_index=1,stage="translate",key="same-content")
        assert (await (await db_conn.execute("SELECT count(*) FROM job WHERE idempotency_key='same-content'")).fetchone())[0]==2
    finally:
        await delete_novel(db_conn,first);await delete_novel(db_conn,second)
