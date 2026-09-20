"""Metadata-only usage accounting. Prompt/prose/key material never enters this table."""
import logging
from novel_llm.provider import SequentialBatchMixin
log=logging.getLogger(__name__)

async def record(conn, novel, stage, result):
    # Zero in older adapters can mean unreported; do not claim a precise zero cost.
    try:
        async with conn.transaction():
            await conn.execute('INSERT INTO account_usage(account_id,novel_id,stage,provider,model,input_tokens,output_tokens) VALUES(current_account(),%s,%s,%s,%s,%s,%s)',
            (novel,stage,result.served_provider,result.served_model,result.input_tokens or None,result.output_tokens or None))
    except Exception as exc:
        log.warning('usage record unavailable: %s',type(exc).__name__)

class UsageProvider(SequentialBatchMixin):
    def __init__(self,inner,conn,novel):
        super().__init__()
        self.inner,self.conn,self.novel=inner,conn,novel
    def __getattr__(self,name):return getattr(self.inner,name)
    async def complete(self,*args,**kwargs):
        result=await self.inner.complete(*args,**kwargs)
        await record(self.conn,self.novel,'pipeline',result)
        return result
