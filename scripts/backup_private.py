#!/usr/bin/env python3
"""Portable DB + object snapshot. Quiesce writers before create; restore only to empty targets.

Requires pg_dump/pg_restore, the migration connection and object-store access. Archives
contain private data and encrypted credentials; store only in the encrypted backup bucket.
The credential encryption key is stored separately, never in this archive.
"""
import argparse,hashlib,io,json,os,subprocess,tarfile,tempfile
from pathlib import Path
import psycopg
from ops_common import objects, object_novel

ROOT=Path(__file__).resolve().parents[1]

def pg(command,*args):
    # libpq reads connection secrets from the environment, never the command line.
    from psycopg.conninfo import conninfo_to_dict
    options=conninfo_to_dict(os.environ['DATABASE_URL'])
    names={'dbname':'PGDATABASE','host':'PGHOST','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT'}
    env={**os.environ,**{names[k]:v for k,v in options.items() if k in names}}
    subprocess.run([command,*args],env=env,check=True)

def create(output):
    client,bucket=objects()
    with tempfile.TemporaryDirectory(prefix='book-backup-') as tmp:
        work=Path(tmp);manifest={'format':1,'objects':[]}
        pg('pg_dump','--enable-row-security','--role=book_backup','--format=custom','--no-owner','--no-acl','--file',str(work/'database.dump'))
        (work/'objects').mkdir()
        # Pipeline output uses translated/<novel>/; bootstrap prose uses novels/.
        # Both prefixes are private library content (§15.3), including saved versions.
        for obj in (obj for prefix in ('novels/','translated/') for obj in client.list_objects(bucket,prefix=prefix,recursive=True)):
            filename=hashlib.sha256(obj.object_name.encode()).hexdigest()
            dest=work/'objects'/filename
            client.fget_object(bucket,obj.object_name,str(dest),version_id=obj.version_id)
            manifest['objects'].append({'key':obj.object_name,'file':filename,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
        manifest['database_sha256']=hashlib.sha256((work/'database.dump').read_bytes()).hexdigest()
        (work/'manifest.json').write_text(json.dumps(manifest))
        # The archive contains private prose and encrypted credentials (§15).
        fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream, tarfile.open(fileobj=stream,mode='w:gz') as archive:
            for name in ['database.dump','objects','manifest.json']:archive.add(work/name,arcname=name)

def restore(archive,journal):
    client,bucket=objects()
    with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as db:
        if db.execute("SELECT to_regclass('public.novel')").fetchone()[0]:raise RuntimeError('restore target database must be empty')
        if next(client.list_objects(bucket,recursive=True),None):raise RuntimeError('restore target bucket must be empty')
        with tempfile.TemporaryDirectory(prefix='book-restore-') as tmp:
            work=Path(tmp)
            with tarfile.open(archive) as tar:tar.extractall(work,filter='data')
            manifest=json.loads((work/'manifest.json').read_text())
            if manifest.get('format')!=1:raise RuntimeError('unsupported snapshot')
            if hashlib.sha256((work/'database.dump').read_bytes()).hexdigest()!=manifest['database_sha256']:raise RuntimeError('database checksum mismatch')
            for obj in manifest['objects']:
                object_novel(obj['key'])
                if obj['file'] != hashlib.sha256(obj['key'].encode()).hexdigest():raise RuntimeError('invalid object manifest')
                path=work/'objects'/obj['file']
                if hashlib.sha256(path.read_bytes()).hexdigest()!=obj['sha256']:raise RuntimeError('object checksum mismatch')
            db.execute((ROOT/'db/hosted/roles.sql').read_text())
            pg('pg_restore','--exit-on-error','--single-transaction','--no-owner','--no-acl','--dbname','',str(work/'database.dump'))
            # Reapply runtime permissions and narrowly owned definer functions for this release.
            db.execute((ROOT/'db/hosted/restore_permissions.sql').read_text())
            db.execute('SET search_path=public;SET row_security=on')
            db.execute('SET ROLE book_maintenance')
            tombstones=json.loads(Path(journal).read_text())
            for account in tombstones['accounts']:
                db.execute('SELECT request_account_deletion(%s)',(account,))
            for novel in tombstones['novels']:
                db.execute('DELETE FROM novel WHERE id=%s',(novel,))
            live={str(r[0]) for r in db.execute("SELECT n.id FROM novel n JOIN account a ON a.id=n.owner_id WHERE a.status='active'")}
            for obj in manifest['objects']:
                if object_novel(obj['key']) in live:client.fput_object(bucket,obj['key'],str(work/'objects'/obj['file']))
            # Sessions and stale claims must never survive a restore.
            db.execute('DELETE FROM account_session;DELETE FROM account_oauth_state')
    print('Restored snapshot; sessions revoked. Reconcile queues and run isolation tests before serving.')

def journal(output):
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        db.execute('SET ROLE book_backup')
        data={'accounts':[str(r[0]) for r in db.execute('SELECT account_id FROM account_deletion_tombstone')],
              'novels':[str(r[0]) for r in db.execute('SELECT novel_id FROM account_cleanup')]}
    Path(output).write_text(json.dumps(data))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('create');p.add_argument('output')
    p=sub.add_parser('restore');p.add_argument('archive');p.add_argument('--deletion-journal',required=True)
    p=sub.add_parser('journal');p.add_argument('output')
    args=parser.parse_args()
    if args.command=='create':create(args.output)
    elif args.command=='journal':journal(args.output)
    else:restore(args.archive,args.deletion_journal)
