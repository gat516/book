#!/usr/bin/env python3
"""Real container HTTP/account smoke. Uses a disposable database and no model calls.

Requires TEST_ADMIN_DATABASE_URL, TEST_REDIS_URL, TEST_OBJECT_ENDPOINT (host:port),
TEST_OBJECT_ACCESS_KEY/SECRET_KEY and built book-{reader,ingest}-hosted:test images.
Google discovery runs; OAuth identity exchange is covered by the auth package tests.
"""
import base64,hashlib,os,secrets,socket,subprocess,sys,time,uuid
from pathlib import Path
import httpx
import psycopg
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict,make_conninfo
from minio import Minio

ROOT=Path(__file__).resolve().parents[2]

def docker(*args,**kwargs):return subprocess.run(['docker',*args],check=True,capture_output=True,text=True,**kwargs).stdout.strip()
def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def main():
    admin=os.environ['TEST_ADMIN_DATABASE_URL'];suffix=secrets.token_hex(5)
    database='http_smoke_'+suffix;bucket='http-smoke-'+suffix
    accounts=[str(uuid.uuid4()),str(uuid.uuid4())];novels=[str(uuid.uuid4()),str(uuid.uuid4())]
    tokens=[secrets.token_urlsafe(32),secrets.token_urlsafe(32)]
    services={};logins=[]
    origin='https://books.example.test';reader_port=port();ingest_port=port();local_port=port()
    def dsn(**kwargs):return make_conninfo(**{**conninfo_to_dict(admin),'dbname':database,**kwargs})
    client=Minio(os.environ['TEST_OBJECT_ENDPOINT'],access_key=os.environ['TEST_OBJECT_ACCESS_KEY'],secret_key=os.environ['TEST_OBJECT_SECRET_KEY'],secure=False)
    env={'BOOK_MODE':'hosted','APP_ORIGIN':origin,'REDIS_URL':os.environ['TEST_REDIS_URL'],
         'OBJECT_STORE_ENDPOINT':os.environ['TEST_OBJECT_ENDPOINT'],'OBJECT_STORE_ACCESS_KEY':os.environ['TEST_OBJECT_ACCESS_KEY'],
         'OBJECT_STORE_SECRET_KEY':os.environ['TEST_OBJECT_SECRET_KEY'],'OBJECT_STORE_BUCKET':bucket,
         'INGEST_INTERNAL_TOKEN':secrets.token_urlsafe(32),'ASKAI_INTERNAL_TOKEN':secrets.token_urlsafe(32),
         'INGEST_PROVIDER_CONFIG_KEY':base64.b64encode(secrets.token_bytes(32)).decode(),
         'GOOGLE_CLIENT_ID':'smoke-test','GOOGLE_CLIENT_SECRET':'smoke-test',
         'LLM_MODEL_ASK':'smoke-test-model','LLM_MODEL_EXTRACT':'smoke-test-model',
         'INGEST_API_URL':f'http://127.0.0.1:{ingest_port}'}
    def start(name,image,extra,command=()):
        values={**env,**extra};container='book-smoke-'+name+'-'+suffix
        docker('run','-d','--name',container,'--network','host',*[arg for key in values for arg in ['-e',key]],image,*command,env={**os.environ,**values})
        services[name]=container
    def wait(url):
        for _ in range(60):
            try:
                if httpx.get(url,timeout=1).status_code==200:return
            except httpx.HTTPError:pass
            time.sleep(1)
        raise RuntimeError('container did not become healthy')
    with psycopg.connect(admin,autocommit=True) as operator:
        try:
            operator.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
            client.make_bucket(bucket)
            subprocess.run([sys.executable,str(ROOT/'db/migrate.py'),'--bootstrap'],check=True,env={**os.environ,'DATABASE_URL':dsn()})
            connections={}
            for service,roles in {'reader':['rls_reader','reader_progress_writer'],'auth':['book_auth'],'ingest':['ingest_writer'],'pipeline':['book_worker'],'askai':['rls_reader']}.items():
                name='smoke_'+service+'_'+suffix;password=secrets.token_urlsafe(24);logins.append(name)
                operator.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
                for role in roles:operator.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(role),sql.Identifier(name)))
                connections[service]=dsn(user=name,password=password)
            with psycopg.connect(dsn(),autocommit=True) as db:
                for i,(account,novel,token) in enumerate(zip(accounts,novels,tokens)):
                    db.execute('INSERT INTO account(id,email) VALUES(%s,%s)',(account,f'{i}@example.test'))
                    db.execute("INSERT INTO novel(id,owner_id,title,source_lang,target_lang,ontology) VALUES(%s,%s,%s,'zh','en','{}')",(novel,account,f'Private {i}'))
                    db.execute("INSERT INTO account_session(token_hash,account_id,csrf_token,expires_at) VALUES(%s,%s,'csrf-test',now()+interval '1 day')",(hashlib.sha256(token.encode()).hexdigest(),account))
            start('ingest','book-ingest-hosted:test',{'DATABASE_URL':connections['ingest'],'LISTEN_ADDR':f':{ingest_port}'})
            start('reader','book-reader-hosted:test',{'DATABASE_URL':connections['reader'],'AUTH_DATABASE_URL':connections['auth'],'READER_LISTEN_ADDR':f':{reader_port}'})
            wait(f'http://127.0.0.1:{reader_port}/healthz');wait(f'http://127.0.0.1:{ingest_port}/healthz')
            def request(method,path,actor=0,csrf=True,**kwargs):
                headers={'Cookie':'__Host-book-session='+tokens[actor],
                         'X-Account-ID':accounts[1-actor],'X-Reader-ID':accounts[1-actor]}
                if csrf:headers.update({'Origin':origin,'X-CSRF-Token':'csrf-test'})
                return httpx.request(method,f'http://127.0.0.1:{reader_port}'+path,headers=headers,timeout=10,**kwargs)
            assert httpx.get(f'http://127.0.0.1:{reader_port}/novels').status_code==401
            for actor in [0,1]:
                response=request('GET','/novels',actor)
                assert response.status_code==200,response.text
                assert novels[actor] in response.text and novels[1-actor] not in response.text
                assert request('GET','/novels/'+novels[1-actor],actor).status_code==404
                assert request('GET',f'/novels/{novels[1-actor]}/provider-config',actor).status_code==404
            assert request('POST','/novels',csrf=False,json={'title':'CSRF'}).status_code==403
            response=request('POST','/novels',json={'title':'Created privately'})
            assert response.status_code==201,response.text
            response=request('PUT','/provider-credentials/gemini',json={'api_key':'fake-test-key'})
            assert response.status_code==200,response.text
            assert 'fake-test-key' not in request('GET','/provider-credentials').text
            with psycopg.connect(dsn()) as db:
                assert db.execute("SELECT account_id::text,key_version FROM provider_credential WHERE provider='gemini'").fetchall()==[(accounts[0],1)]
                assert db.execute("SELECT owner_id::text FROM novel WHERE title='Created privately'").fetchone()[0]==accounts[0]
            response=request('PATCH','/queue',json={'mode':'paused'})
            assert response.status_code==200,response.text
            assert request('GET','/queue').json()['mode']=='paused'
            assert request('GET','/queue',1).json()['mode']=='all'
            assert httpx.get(f'http://127.0.0.1:{ingest_port}/provider-credentials').status_code==401
            # Exercise real Python startup with the worker/Ask AI database roles.
            cache=redis.Redis.from_url(os.environ['TEST_REDIS_URL'])
            cache.delete('jobs:worker:heartbeat')
            start('pipeline','book-python-hosted:test',{'DATABASE_URL':connections['pipeline'],
                'TEXTPROC_BACKEND':'python','PROVIDER_CONFIG_ENCRYPTION_KEY':env['INGEST_PROVIDER_CONFIG_KEY']})
            for _ in range(30):
                if cache.exists('jobs:worker:heartbeat'):break
                time.sleep(1)
            else:raise AssertionError('restricted worker did not start')
            ask_port=port()
            start('askai','book-python-hosted:test',{'DATABASE_URL':connections['askai'],'ASKAI_PORT':str(ask_port),
                'PROVIDER_CONFIG_ENCRYPTION_KEY':env['INGEST_PROVIDER_CONFIG_KEY']},('python','-m','askai'))
            wait(f'http://127.0.0.1:{ask_port}/healthz')
            # Local mode works on the new schema with no OAuth session or browser actor.
            start('local','book-reader-hosted:test',{'BOOK_MODE':'local','DATABASE_URL':connections['reader'],'READER_LISTEN_ADDR':f':{local_port}'})
            wait(f'http://127.0.0.1:{local_port}/healthz')
            assert httpx.get(f'http://127.0.0.1:{local_port}/novels').status_code==200
            assert httpx.get(f'http://127.0.0.1:{local_port}/auth/session').json()['local'] is True
            response=request('DELETE','/auth/account')
            assert response.status_code==202,response.text
            assert request('GET','/novels').status_code==401
            assert request('GET','/novels',1).status_code==200
            print('PASS: hosted HTTP isolation, forged headers, CSRF, private creation/keys/queue, account deletion, localhost without login.')
        except Exception:
            for name,container in services.items():
                print(name,file=sys.stderr)
                subprocess.run(['docker','logs','--tail','15',container],check=False)
            raise
        finally:
            for container in services.values():docker('rm','-f',container)
            operator.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(database)))
            for name in logins:operator.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(name)))
            if client.bucket_exists(bucket):client.remove_bucket(bucket)

if __name__=='__main__':main()
