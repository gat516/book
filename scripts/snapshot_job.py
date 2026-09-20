#!/usr/bin/env python3
"""Scheduled portable snapshot with a bounded writer maintenance window (§15).

Requires only deployment scale + pod-list Kubernetes permissions. A killed node may
leave writers at zero; deploy/hosted/README.md documents the explicit recovery command.
"""
import datetime,json,os,signal,ssl,tempfile,time,urllib.request
import psycopg
from pathlib import Path
from backup_private import create,journal
from ops_common import objects

TOKEN=Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
ROOT='https://'+os.environ.get('KUBERNETES_SERVICE_HOST','kubernetes.default.svc')+':443'

def kube(path,method='GET',data=None):
    ctx=ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
    headers={'Authorization':'Bearer '+TOKEN.read_text().strip(),'Content-Type':'application/merge-patch+json'}
    req=urllib.request.Request(ROOT+path,data=json.dumps(data).encode() if data is not None else None,headers=headers,method=method)
    with urllib.request.urlopen(req,context=ctx,timeout=15) as r:return json.load(r)

def scale(name,replicas):
    kube(f'/apis/apps/v1/namespaces/book/deployments/{name}/scale','PATCH',{'spec':{'replicas':replicas}})

def main():
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    saved={}
    try:
        for name in ['ingest-api','scraper','pipeline']:
            saved[name]=kube(f'/apis/apps/v1/namespaces/book/deployments/{name}/scale')['spec']['replicas']
            scale(name,0)
        deadline=time.monotonic()+180
        while True:
            pods=kube('/api/v1/namespaces/book/pods')['items']
            if not any(p['metadata'].get('labels',{}).get('app') in saved for p in pods):break
            if time.monotonic()>deadline:raise TimeoutError('writers did not stop')
            time.sleep(2)
        # Serialize the whole snapshot/journal upload with the deletion sweeper so a
        # snapshot cannot replace a newer erasure journal or race object removal.
        with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as lock, tempfile.TemporaryDirectory(prefix='snapshot-') as tmp:
            lock.execute('SELECT pg_advisory_lock(739031)')
            path=Path(tmp)/'snapshot.tar.gz';deletions=Path(tmp)/'deletions.json'
            create(path);journal(deletions)
            client,_=objects();bucket=os.environ['BACKUP_BUCKET']
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            client.fput_object(bucket,'snapshots/'+stamp+'.tar.gz',str(path))
            client.fput_object(bucket,'deletions/current.json',str(deletions),content_type='application/json')
            print('Snapshot uploaded:',stamp)
    finally:
        for name,replicas in saved.items():scale(name,replicas)

if __name__=='__main__':main()
