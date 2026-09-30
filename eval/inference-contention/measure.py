#!/usr/bin/env python3
"""Bounded operator experiment, run on the existing k3s node (spec §14, §15).

Uses the running Ask AI HTTP service and the worker's ordinary queue. Requires
explicit authorization for paid inference and processing the specified chapter.
Emits only metadata; no prompts, answers, credentials, or chapter bodies.
Ask latency excludes the browser/reader API/Cloudflare hop. Redis stage overlap
is evidence of overlapping chapter work, not exact provider request overlap.
"""

import argparse
import json
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone


COMMON = '''
import json,os,time,psycopg,redis
db=psycopg.connect(os.environ["DATABASE_URL"],autocommit=True)
db.execute("SET ROLE book_worker")
db.execute("SET default_transaction_read_only=on")
r=redis.Redis.from_url(os.environ["REDIS_URL"],decode_responses=True)
'''

WATCH = COMMON + '''
until=time.monotonic()+1800
while time.monotonic()<until:
 pipe=r.pipeline(transaction=True)
 pipe.lrange("jobs:processing",0,-1)
 pipe.hgetall("jobs:processing:stage")
 pipe.lrange("jobs:pending",0,-1)
 processing,stages,pending=pipe.execute()
 active=[]
 for raw in processing:
  m=json.loads(raw)
  if m.get("novel_id")==novel:
   active.append({"chapter":m["chapter_index"],"stage":stages.get(raw,"unknown")})
 print(json.dumps({"at":time.time(),"active":active,
  "pending":[json.loads(x)["chapter_index"] for x in pending if json.loads(x).get("novel_id")==novel]}),flush=True)
 time.sleep(0.5)
'''

SNAPSHOT = COMMON + '''
with db.transaction():
 db.execute("SELECT set_config('app.account_id',%s,true)",(account,))
 catalog=db.execute("SELECT mode,focus_novel_id FROM worker_catalog() WHERE novel_id=%s AND account_id=%s",(novel,account)).fetchone()
 progress=db.execute("SELECT max(current_chapter) FROM reader_progress WHERE novel_id=%s",(novel,)).fetchone()[0]
 print(json.dumps({"catalog":catalog,"progress":progress,
  "chapter":db.execute("SELECT chapter_index,status,translation_ready,enrichment_discarded,provider_retry_category FROM chapter WHERE novel_id=%s AND chapter_index=%s",(novel,chapter)).fetchone(),
  "usage":db.execute("SELECT stage,provider,model,count(*),sum(input_tokens),sum(output_tokens) FROM account_usage WHERE novel_id=%s AND created_at>=to_timestamp(%s) GROUP BY stage,provider,model",(novel,since)).fetchall(),
  "usage_events":db.execute("SELECT extract(epoch from created_at),stage,provider,model,input_tokens,output_tokens FROM account_usage WHERE novel_id=%s AND created_at>=to_timestamp(%s) ORDER BY created_at,id",(novel,since)).fetchall(),
  "failures":db.execute("SELECT stage,error_code,count(*) FROM chapter_failure WHERE novel_id=%s AND occurred_at>=to_timestamp(%s) GROUP BY stage,error_code",(novel,since)).fetchall(),
  "retries":db.execute("SELECT chapter_index,provider_retry_attempts,provider_retry_category FROM chapter WHERE novel_id=%s AND provider_retry_attempts>0",(novel,)).fetchall()},default=str))
'''

ENQUEUE = COMMON + '''
with db.transaction():
 db.execute("SELECT set_config('app.account_id',%s,true)",(account,))
 catalog=db.execute("SELECT mode,focus_novel_id FROM worker_catalog() WHERE novel_id=%s AND account_id=%s",(novel,account)).fetchone()
 if not catalog or catalog[0]=="paused" or (catalog[0]=="focused" and str(catalog[1])!=novel):
  raise RuntimeError("account queue is not enabled for this novel")
 row=db.execute("SELECT status,translation_ready,enrichment_discarded,provider_retry_at FROM chapter WHERE novel_id=%s AND chapter_index=%s",(novel,chapter)).fetchone()
 if not row or row[0]!="ingested" or row[1] or row[2] or row[3]:
  raise RuntimeError("target is not an unpaused, unprocessed ingested chapter")
 # No retranslation, pause changes, retry resets, or published-data changes.
 lua=''' + repr('''
for _,key in ipairs(KEYS) do
 for _,raw in ipairs(redis.call('LRANGE',key,0,-1)) do
  local m=cjson.decode(raw)
  if m.novel_id==ARGV[1] then return 0 end
 end
end
redis.call('LPUSH',KEYS[1],ARGV[2]); return 1
''') + '''
 added=r.eval(lua,2,"jobs:pending","jobs:processing",novel,json.dumps({"novel_id":novel,"chapter_index":chapter,"priority":False,"enrichment":False,"retranslate":False}))
 print(json.dumps({"enqueued":bool(added),"chapter":chapter}))
'''

ASK = '''
import os,json,time,httpx,hashlib
started=time.time()
timer=time.perf_counter()
try:
 response=httpx.post("http://127.0.0.1:8082/ask",
  json={"novel_id":novel,"at":gate,"question":"Summarize the main character's current situation in three sentences, using only the chapters available to me."},
  headers={"Authorization":"Bearer "+os.environ["ASKAI_INTERNAL_TOKEN"],"X-Account-ID":account},timeout=180)
 finished=time.time()
 data=response.json()
 category=data.get("category")
 if category not in {"rate_limited","quota_exhausted","model_server_error","credential_missing","credential_rejected","model_not_available","provider_invalid_json","provider_bad_request"}:category=None
 source_fingerprint=hashlib.sha256(json.dumps(data.get("retrieved_sources",[]),sort_keys=True).encode()).hexdigest()[:16]
 print(json.dumps({"started":started,"finished":finished,"seconds":round(time.perf_counter()-timer,3),"status":response.status_code,"category":category,"served_by":data.get("served_by"),"sources":len(data.get("retrieved_sources",[])),"source_fingerprint":source_fingerprint,"answer_chars":len(data.get("answer","")),"gate":data.get("at")}))
except Exception as exc:
 print(json.dumps({"started":started,"finished":time.time(),"seconds":round(time.perf_counter()-timer,3),"error_type":type(exc).__name__}))
'''


def emit(event, **data):
    print(json.dumps({"event": event, **data}), flush=True)


def command(args, service, source):
    bindings = {"novel": args.novel, "account": args.account,
                "chapter": args.chapter, "gate": args.gate, "since": args.since}
    source = "\n".join(f"{k}={v!r}" for k, v in bindings.items()) + "\n" + source
    return ["k3s", "kubectl", "-n", args.namespace, "exec", "deployment/" + service,
            "--", "python", "-u", "-c", source]


def remote(args, service, source):
    result = subprocess.run(command(args, service, source), capture_output=True, text=True, timeout=210)
    if result.returncode:
        # Do not forward raw exceptions: SDK/DB diagnostics can contain secrets.
        raise RuntimeError(f"{service} measurement helper exited {result.returncode}")
    return json.loads(result.stdout)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("novel", "account"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--chapter", type=int, required=True)
    p.add_argument("--gate", type=int, required=True)
    p.add_argument("--namespace", default="book")
    p.add_argument("--baseline", type=int, default=6)
    p.add_argument("--recovery", type=int, default=6)
    p.add_argument("--max-during", type=int, default=12)
    p.add_argument("--since", type=float, default=time.time())
    p.add_argument("--enqueue", action="store_true", help="explicitly process the one specified chapter")
    args = p.parse_args()
    if not (1 <= args.baseline <= 12 and 1 <= args.recovery <= 12 and 1 <= args.max_during <= 20):
        p.error("sample counts exceed the bounded experiment")
    initial = remote(args, "pipeline", SNAPSHOT)
    if initial["progress"] is None or args.gate > initial["progress"] or args.gate < 1:
        p.error("requested gate exceeds saved reader progress")
    if args.enqueue:
        target = initial["chapter"]
        if not target or target[1] != "ingested" or target[2] or target[3] or target[4]:
            p.error("target chapter is no longer eligible; stop before paid sampling")
    emit("start", utc=datetime.now(timezone.utc).isoformat(), metadata=initial,
         scope="Ask AI service HTTP latency; 0.5-second Redis chapter-stage observations")
    observations = []
    watch = subprocess.Popen(command(args, "pipeline", WATCH), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)

    def read_watch():
        for line in watch.stdout:
            observations.append(json.loads(line))

    reader = threading.Thread(target=read_watch, daemon=True)
    reader.start()
    results = []
    last_started = 0

    def active():
        if not observations or time.time() - observations[-1]["at"] > 5:
            raise RuntimeError("queue observer unavailable")
        return bool(observations[-1]["active"] or observations[-1]["pending"])

    def sample(phase):
        nonlocal last_started
        time.sleep(max(0, 7 - (time.time() - last_started)))
        last_started = time.time()
        result = remote(args, "askai", ASK)
        # Allow the observer's half-second tick to catch the request's final edge.
        time.sleep(0.6)
        ticks = [x for x in observations if result["started"] <= x["at"] <= result["finished"]]
        busy = [x for x in ticks if x["active"]]
        result.update(phase=phase, active_ticks=len(busy), observed_ticks=len(ticks),
                      stages=sorted({m["stage"] for x in busy for m in x["active"]}),
                      chapters=sorted({m["chapter"] for x in busy for m in x["active"]}))
        results.append(result)
        emit("sample", **result)
        if result.get("status") != 200 or not result.get("served_by") or result.get("gate") != args.gate:
            raise RuntimeError("probe did not produce an authorized real model answer; stopping paid samples")
        return result

    try:
        deadline = time.monotonic() + 15
        while not observations and time.monotonic() < deadline:
            time.sleep(0.1)
        if active():
            raise RuntimeError("chapter work is already active; cannot establish an idle baseline")
        for _ in range(args.baseline):
            result = sample("baseline")
            if result["active_ticks"]:
                raise RuntimeError("baseline overlapped unexpected chapter work")
        if args.enqueue:
            added = remote(args, "pipeline", ENQUEUE)
            emit("enqueue", **added)
            if not added["enqueued"]:
                raise RuntimeError("novel already has queued work; bounded enqueue refused")
            deadline = time.monotonic() + 30
            while not active() and time.monotonic() < deadline:
                time.sleep(0.5)
            if not active():
                raise RuntimeError("chapter was not observed starting")
            for _ in range(args.max_during):
                if not active():
                    break
                sample("during")
            deadline = time.monotonic() + 600
            idle_since = None
            while time.monotonic() < deadline:
                if active():
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since >= 3:
                    break
                time.sleep(1)
            if idle_since is None or time.monotonic() - idle_since < 3:
                raise RuntimeError("chapter still active; recovery cannot be measured yet")
            for _ in range(args.recovery):
                result = sample("recovery")
                if result["active_ticks"]:
                    raise RuntimeError("recovery overlapped unexpected work")
        transitions = []
        for tick in observations:
            if not transitions or tick["active"] != transitions[-1]["active"]:
                transitions.append({"at": tick["at"], "active": tick["active"]})
        emit("finish", metadata=remote(args, "pipeline", SNAPSHOT), transitions=transitions,
             phases={phase: {"n":len(xs), "median_seconds":round(statistics.median(xs),3),
                             "min_seconds":min(xs), "max_seconds":max(xs)}
                     for phase in ("baseline","during","recovery")
                     if (xs := [r["seconds"] for r in results if r["phase"] == phase])})
    finally:
        watch.terminate()
        try:
            watch.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watch.kill()
        reader.join(timeout=1)


if __name__ == "__main__":
    main()
