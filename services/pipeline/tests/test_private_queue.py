"""Real Lua against isolated Redis; never flushes a shared database."""
import json
import os
import uuid
import pytest
from redis.asyncio import Redis
from pipeline import queue

async def test_fair_claims_pause_owner_and_recovery():
    url=os.getenv('PRIVATE_TEST_REDIS_URL')
    if not url:pytest.skip('PRIVATE_TEST_REDIS_URL required')
    redis=Redis.from_url(url,decode_responses=True)
    prefix='private-test:'+str(uuid.uuid4())+':'
    keys=[prefix+str(i) for i in range(len(queue.KEYS))]
    a,b=str(uuid.uuid4()),str(uuid.uuid4())
    novel_a,novel_a2,novel_b=str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4())
    catalog={novel_a:{'account':a,'mode':'all','focus':''},novel_a2:{'account':a,'mode':'all','focus':''},novel_b:{'account':b,'mode':'all','focus':''}}
    messages=[json.dumps({'novel_id':n,'chapter_index':ch}) for n,ch in [(novel_a,1),(novel_a,2),(novel_a2,1),(novel_b,1),(novel_b,2)]]
    async def claim(t):return await redis.eval(queue.CLAIM_PRIVATE,len(keys),*keys,str(t),json.dumps(catalog))
    try:
        await redis.lpush(keys[0],*messages)
        first=await claim(1);second=await claim(2)
        assert catalog[json.loads(first)['novel_id']]['account'] != catalog[json.loads(second)['novel_id']]['account']
        assert await claim(3) is None # only one in flight per account
        assert await redis.eval(queue.REAP,len(keys),*keys,first,'100','10')==1
        assert await claim(101) is not None # abandoned owner resumes
        await redis.delete(*keys)
        catalog[novel_a]['mode']='paused';catalog[novel_a2]['mode']='paused'
        await redis.lpush(keys[0],*messages)
        assert json.loads(await claim(102))['novel_id']==novel_b
        assert await claim(103) is None
    finally:
        await redis.delete(*keys)
        await redis.hdel('jobs:account:last-served',a,b)
        await redis.aclose()
