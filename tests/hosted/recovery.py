#!/usr/bin/env python3
"""Exercise real backup/restore and versioned erasure using disposable DBs/buckets.

TEST_ADMIN_DATABASE_URL must point to a test PostgreSQL cluster. OBJECT_STORE_* and
TEST_REDIS_URL must point to disposable infrastructure. No production defaults.
Run in the Python production image (includes PostgreSQL 16 tools).
"""
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import redis
from minio.deleteobjects import DeleteObject
from minio.versioningconfig import ENABLED, VersioningConfig

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
from backup_private import create,restore
from cleanup_accounts import sweep
from ops_common import objects


def main():
    admin_url=os.environ['TEST_ADMIN_DATABASE_URL']
    cache=redis.Redis.from_url(os.environ['TEST_REDIS_URL'],decode_responses=True)
    client,_=objects()
    suffix=secrets.token_hex(5)
    source='recovery_source_'+suffix;target='recovery_target_'+suffix
    buckets=['recovery-source-'+suffix,'recovery-target-'+suffix,'recovery-journal-'+suffix]
    login='recovery_backup_'+suffix;password=secrets.token_urlsafe(24)
    accounts=[uuid.uuid4(),uuid.uuid4()];novels=[uuid.uuid4(),uuid.uuid4()]
    def url(db,**kwargs):return make_conninfo(**{**conninfo_to_dict(admin_url),'dbname':db,**kwargs})
    with psycopg.connect(admin_url,autocommit=True) as operator, tempfile.TemporaryDirectory() as tmp:
        try:
            for db in [source,target]:operator.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(db)))
            for bucket in buckets:
                client.make_bucket(bucket)
                client.set_bucket_versioning(bucket,VersioningConfig(ENABLED))
            os.environ['DATABASE_URL']=url(source)
            os.environ['OBJECT_STORE_BUCKET']=buckets[0]
            os.environ['BACKUP_BUCKET']=buckets[2]
            subprocess.run([sys.executable,str(ROOT/'db/migrate.py'),'--bootstrap'],check=True)
            with psycopg.connect(url(source),autocommit=True) as db:
                for i,(account,novel) in enumerate(zip(accounts,novels)):
                    db.execute('INSERT INTO account(id,email) VALUES(%s,%s)',(account,f'{i}@example.test'))
                    db.execute("INSERT INTO novel(id,owner_id,title,source_lang,target_lang,ontology) VALUES(%s,%s,%s,'zh','en','{}')",(novel,account,f'Private {i}'))
                    db.execute("INSERT INTO chapter(novel_id,chapter_index,raw_hash,raw_uri,source_meta) VALUES(%s,1,'hash','test','{}')",(novel,))
                    db.execute("INSERT INTO chapter_fact(novel_id,chapter_index,prompt_version,ordinal,text,source_hash,requested_model) VALUES(%s,1,'test',1,'Private fact','hash','test')",(novel,))
                    for content in [b'old private prose',b'current private prose']:
                        client.put_object(buckets[0],f'novels/{novel}/raw.txt',io.BytesIO(content),len(content))
                db.execute("INSERT INTO account_session(token_hash,account_id,csrf_token,expires_at) VALUES(%s,%s,'test',now()+interval '1 day')",(hashlib.sha256(b'test').hexdigest(),accounts[0]))
            operator.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}').format(sql.Identifier(login),sql.Literal(password)))
            operator.execute(sql.SQL('GRANT book_backup TO {}').format(sql.Identifier(login)))
            os.environ['DATABASE_URL']=url(source,user=login,password=password)
            with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as db:
                db.execute('SET ROLE book_backup')
                assert db.execute('SELECT count(*) FROM novel').fetchone()[0]==2
                try:db.execute("UPDATE novel SET title='forbidden'")
                except psycopg.errors.InsufficientPrivilege:pass
                else:raise AssertionError('backup role can write')
            archive=Path(tmp)/'snapshot.tar.gz'
            create(archive)
            assert archive.stat().st_mode & 0o077 == 0
            # Delete B after the snapshot. Its erasure must survive restoring the old DB.
            with psycopg.connect(url(source),autocommit=True) as db:
                db.execute('SELECT request_account_deletion(%s)',(accounts[1],))
                db.execute('UPDATE account_cleanup SET retry_at=now() WHERE account_id=%s',(accounts[1],))
                db.execute('SET ROLE book_cleanup')
                sweep(db,client,buckets[0],cache)
                assert not list(client.list_objects(buckets[0],prefix=f'novels/{novels[1]}/',recursive=True,include_version=True))
                assert len(list(client.list_objects(buckets[0],prefix=f'novels/{novels[0]}/',recursive=True,include_version=True)))==2
                db.execute('UPDATE account_cleanup SET retry_at=now() WHERE account_id=%s',(accounts[1],))
                sweep(db,client,buckets[0],cache)
                assert not db.execute('SELECT FROM account WHERE id=%s',(accounts[1],)).fetchone()
            deletion_file=Path(tmp)/'deletions.json'
            client.fget_object(buckets[2],'deletions/current.json',str(deletion_file))
            assert str(accounts[1]) in json.loads(deletion_file.read_text())['accounts']
            os.environ['DATABASE_URL']=url(target)
            os.environ['OBJECT_STORE_BUCKET']=buckets[1]
            restore(archive,deletion_file)
            with psycopg.connect(url(target),autocommit=True) as db:
                assert db.execute('SELECT id FROM novel').fetchall()==[(novels[0],)]
                assert db.execute('SELECT count(*) FROM chapter_fact').fetchone()[0]==1
                assert db.execute('SELECT count(*) FROM account_session').fetchone()[0]==0
                db.execute('SET ROLE rls_reader')
                db.execute("SELECT set_config('app.account_id',%s,false)",(str(accounts[0]),))
                assert db.execute('SELECT id FROM novel').fetchall()==[(novels[0],)]
                db.execute("SELECT set_config('app.account_id',%s,false)",(str(accounts[1]),))
                assert not db.execute('SELECT id FROM novel').fetchall()
            restored=list(client.list_objects(buckets[1],recursive=True))
            assert [obj.object_name for obj in restored]==[f'novels/{novels[0]}/raw.txt']
            response=client.get_object(buckets[1],restored[0].object_name)
            try:assert response.read()==b'current private prose'
            finally:response.close();response.release_conn()
            print('PASS: restricted backup, restore permissions, private prose hashes, session revocation, version erasure, deletion replay.')
        finally:
            for db in [source,target]:operator.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(db)))
            operator.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(login)))
            for bucket in buckets:
                if client.bucket_exists(bucket):
                    versions=client.list_objects(bucket,recursive=True,include_version=True)
                    errors=list(client.remove_objects(bucket,(DeleteObject(o.object_name,o.version_id) for o in versions)))
                    if errors:raise RuntimeError('test bucket cleanup failed')
                    client.remove_bucket(bucket)

if __name__=='__main__':main()
