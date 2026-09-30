#!/usr/bin/env python3
"""Approved deployed smoke test: <=6 paid Ask AI calls, no chapter processing.

Run on the k3s node. Output is metadata only; never prints provider errors, keys,
prompts, answers, retrieved text or reservation IDs. Operator approval is external
to this script and must cover the selected account's private context/paid calls.
"""
import argparse
import json
import statistics
import subprocess
import time
from uuid import UUID


SNAPSHOT = '''
import os,psycopg,redis
r=redis.Redis.from_url(os.environ['REDIS_URL'])
db=psycopg.connect(os.environ['DATABASE_URL'],autocommit=True)
db.execute('SET ROLE book_worker')
db.execute('SET default_transaction_read_only=on')
with db.transaction():
 db.execute("SELECT set_config('app.account_id',%s,true)",(account,))
 progress=db.execute('SELECT max(current_chapter) FROM reader_progress WHERE novel_id=%s',(novel,)).fetchone()[0]
 chapters=db.execute('SELECT chapter_index,status,translation_ready FROM chapter WHERE novel_id=%s AND chapter_index>%s ORDER BY chapter_index',(novel,gate)).fetchall()
 usage=db.execute('SELECT stage,provider,model,input_tokens,output_tokens FROM account_usage WHERE novel_id=%s AND created_at>=to_timestamp(%s) ORDER BY created_at,id',(novel,since)).fetchall()
 failures=db.execute('SELECT error_code,count(*) FROM chapter_failure WHERE novel_id=%s AND occurred_at>=to_timestamp(%s) GROUP BY error_code',(novel,since)).fetchall()
 print(json.dumps({'progress':progress,'chapters':chapters,'pending':r.llen('jobs:pending'),'processing':r.llen('jobs:processing'),'usage':usage,'failures':failures}))
'''

CONTRACT = '''
import os,asyncio,grpc
from novel_llm import gateway_pb2 as pb,gateway_pb2_grpc as rpc
async def check():
 assert os.environ.get('LLM_GATEWAY_ADMISSION_ADDR')=='gateway-admission:8081'
 assert account in os.environ.get('LLM_GATEWAY_ADMISSION_ACCOUNTS','').split(',')
 async with grpc.aio.insecure_channel('gateway-admission:8081') as channel:
  client=rpc.AdmissionStub(channel)
  request=pb.StatusRequest(tenant=account,provider='deepseek',model='deepseek-v4-flash')
  assert (await client.GetStatus(request,timeout=3)).pending_reservations==0
  grants=[]
  try:
   for priority in (pb.BATCH,pb.INTERACTIVE):
    grant=await client.Reserve(pb.ReserveRequest(tenant=account,provider='deepseek',model='deepseek-v4-flash',backend=pb.HOSTED,priority=priority,no_fallback=True),timeout=3)
    assert grant.allowed
    grants.append(grant)
   denied=await client.Reserve(pb.ReserveRequest(tenant=account,provider='deepseek',model='deepseek-v4-flash',backend=pb.HOSTED,no_fallback=True),timeout=3)
   if denied.allowed: grants.append(denied)
   assert not denied.allowed
  finally:
   for grant in grants:
    assert (await client.Settle(pb.SettleRequest(reservation_id=grant.reservation_id,failed=True),timeout=3)).settled
  status=await client.GetStatus(request,timeout=3)
  assert status.pending_reservations==0 and status.rpm_remaining==2
  try:
   await rpc.GatewayStub(channel).Complete(pb.CompletionRequest()).read()
  except grpc.aio.AioRpcError as exc:
   assert exc.code()==grpc.StatusCode.UNIMPLEMENTED
  else: raise AssertionError('proxy surface unexpectedly registered')
  print(json.dumps({'capacity':2,'over_capacity_rejected':True,'pending_after_settlement':0,'proxy_disabled':True}))
asyncio.run(check())
'''

PIPELINE = '''
import os,asyncio,psycopg
from pipeline.worker import Worker
from pipeline.config import Config
from novel_llm.gateway_admission import AdmissionProvider
async def check():
 conn=await psycopg.AsyncConnection.connect(os.environ['DATABASE_URL'],autocommit=True)
 try:
  await conn.execute('SET ROLE book_worker')
  await conn.execute('SET default_transaction_read_only=on')
  await conn.execute("SELECT set_config('app.account_id',%s,false)",(account,))
  worker=Worker.__new__(Worker)
  worker.cfg=Config.load();worker.db=conn;worker.redis=None
  worker._provider_cache={};worker._provider_rows={}
  provider,*_=await worker._provider_for_novel(novel)
  try:
   assert isinstance(provider,AdmissionProvider) and provider._tenant==account
   print(json.dumps({'pipeline_uses_admission':True,'provider_calls':0}))
  finally: await provider.aclose()
 finally: await conn.close()
asyncio.run(check())
'''

ASK = '''
import os,httpx,hashlib,time
timer=time.perf_counter()
try:
 response=httpx.post('http://127.0.0.1:8082/ask',json={'novel_id':novel,'at':gate,'question':"Summarize the main character's current situation in three sentences, using only the chapters available to me."},headers={'Authorization':'Bearer '+os.environ['ASKAI_INTERNAL_TOKEN'],'X-Account-ID':account},timeout=150)
 data=response.json()
 category=data.get('category')
 if category not in {'rate_limited','quota_exhausted','model_server_error','credential_missing','credential_rejected','model_not_available','provider_invalid_json','provider_bad_request'}: category=None
 print(json.dumps({'seconds':round(time.perf_counter()-timer,3),'status':response.status_code,'category':category,'gate':data.get('at'),'served_by':data.get('served_by'),'sources':len(data.get('retrieved_sources',[])),'source_fingerprint':hashlib.sha256(json.dumps(data.get('retrieved_sources',[]),sort_keys=True).encode()).hexdigest()[:16],'answer_chars':len(data.get('answer',''))}))
except Exception as exc:
 print(json.dumps({'seconds':round(time.perf_counter()-timer,3),'error_type':type(exc).__name__}))
'''


def remote(args, service, source, since):
    bindings = {'account': args.account, 'novel': args.novel, 'gate': args.gate, 'since': since}
    program = 'import json\n' + '\n'.join(f'{key}={value!r}' for key, value in bindings.items()) + '\n' + source
    result = subprocess.run(['k3s','kubectl','-n',args.namespace,'exec','-i','deployment/'+service,'--','python','-'],
                            input=program,text=True,capture_output=True,timeout=180)
    if result.returncode:
        raise RuntimeError(f'{service} smoke helper failed; raw diagnostic suppressed')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True, type=lambda s: str(UUID(s)))
    parser.add_argument('--novel', required=True, type=lambda s: str(UUID(s)))
    parser.add_argument('--gate', required=True, type=int)
    parser.add_argument('--requests', required=True, type=int, choices=range(1,7))
    parser.add_argument('--namespace', default='book')
    args = parser.parse_args()
    since = time.time()
    def emit(event, **data): print(json.dumps({'event':event, **data}),flush=True)
    initial = remote(args,'pipeline',SNAPSHOT,since)
    assert initial['progress'] is not None and 0 < args.gate <= initial['progress']
    assert initial['pending'] == initial['processing'] == 0
    emit('start', since=since, authorized_requests=args.requests, metadata=initial)
    emit('contract', **remote(args,'askai',CONTRACT,since))
    emit('pipeline_wiring', **remote(args,'pipeline',PIPELINE,since))
    samples=[]
    for index in range(args.requests):
        if index: time.sleep(7)
        state=remote(args,'pipeline',SNAPSHOT,since)
        assert state['progress'] >= args.gate and state['chapters']==initial['chapters']
        assert state['pending']==state['processing']==0
        sample=remote(args,'askai',ASK,since)
        emit('sample', index=index+1, **sample)
        samples.append(sample)
        if sample.get('status') != 200 or sample.get('gate') != args.gate or not sample.get('served_by'):
            raise RuntimeError('smoke request failed; stopping further paid requests')
    final=remote(args,'pipeline',SNAPSHOT,since)
    assert final['chapters']==initial['chapters'] and final['progress']==initial['progress']
    assert final['pending']==final['processing']==0 and not final['failures']
    emit('finish', requests=len(samples), median_seconds=statistics.median(s['seconds'] for s in samples),
         chapter_processing_started=False, metadata=final)


if __name__=='__main__': main()
