#!/usr/bin/env python3
"""Durable deletion sweeper. Run every five minutes as book_cleanup, never an HTTP role.

A database advisory lock makes concurrent/restarted sweepers safe. Two sweeps an hour
apart remove versions and late cancelled writes. Backups expire under retention;
restoring a backup requires replaying the current deletion journal before serving it.
"""
import io
import json
import logging
import os
import psycopg
import redis
from minio.deleteobjects import DeleteObject
from ops_common import objects

log=logging.getLogger(__name__)


def purge_objects(client,bucket,novel):
    for prefix in [f'novels/{novel}/']:
        versions=client.list_objects(bucket,prefix=prefix,recursive=True,include_version=True)
        errors=list(client.remove_objects(bucket,(DeleteObject(o.object_name,o.version_id) for o in versions)))
        if errors:raise RuntimeError('object deletion incomplete')


def purge_redis(client,account,novel):
    # Only this novel's content caches are removed; shared queue lists are edited atomically.
    for pattern in [f'llm:result:{account}:{novel}:*',f'translate:preview:{novel}:*']:
        for key in client.scan_iter(match=pattern,count=100):client.unlink(key)
    client.eval('''for _,key in ipairs(KEYS) do
      for _,raw in ipairs(redis.call('LRANGE',key,0,-1)) do
       local ok,m=pcall(cjson.decode,raw)
       if ok and type(m)=='table' and m.novel_id==ARGV[1] then redis.call('LREM',key,0,raw) end
      end
    end return 1''',1,'jobs:pending',str(novel))


def sweep(db,client,bucket,cache):
    if not db.execute('SELECT pg_try_advisory_lock(739031)').fetchone()[0]:return
    try:
        if os.getenv('BACKUP_BUCKET'):
            # Persist erasure intent outside the DB before completing any deletion.
            data=json.dumps({'accounts':[str(r[0]) for r in db.execute('SELECT account_id FROM account_deletion_tombstone')],
                             'novels':[str(r[0]) for r in db.execute('SELECT novel_id FROM account_cleanup')]}).encode()
            client.put_object(os.environ['BACKUP_BUCKET'],'deletions/current.json',io.BytesIO(data),len(data),content_type='application/json')
        rows=db.execute('SELECT id,account_id,novel_id,attempts FROM account_cleanup WHERE completed_at IS NULL AND retry_at<=now() ORDER BY id LIMIT 50').fetchall()
        for ident,account,novel,attempts in rows:
            try:
                # Delete the book first: workers cannot publish after this authorization change.
                db.execute('DELETE FROM novel WHERE id=%s AND owner_id=%s',(novel,account))
                purge_objects(client,bucket,novel)
                purge_redis(cache,account,novel)
                db.execute("UPDATE account_cleanup SET attempts=attempts+1,retry_at=now()+interval '1 hour',completed_at=CASE WHEN attempts>=1 THEN now() ELSE NULL END WHERE id=%s",(ident,))
            except Exception as exc:
                log.warning('cleanup %s deferred (%s)',ident,type(exc).__name__)
                db.execute("UPDATE account_cleanup SET retry_at=now()+interval '5 minutes' WHERE id=%s",(ident,))
        db.execute("DELETE FROM account a WHERE status='deleting' AND NOT EXISTS(SELECT FROM novel n WHERE n.owner_id=a.id) AND NOT EXISTS(SELECT FROM account_cleanup c WHERE c.account_id=a.id AND c.completed_at IS NULL)")
    finally:db.execute('SELECT pg_advisory_unlock(739031)')


def main():
    logging.basicConfig(level=logging.INFO)
    client,bucket=objects();cache=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
    with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as db:
        db.execute('SET ROLE book_cleanup')
        sweep(db,client,bucket,cache)
if __name__=='__main__':main()
