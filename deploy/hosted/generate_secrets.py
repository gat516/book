#!/usr/bin/env python3
"""Generate service-secret JSON and provision DB logins, using an operator connection.

Requires DATABASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET. Importing a local library
also requires its PROVIDER_CONFIG_ENCRYPTION_KEY. --fresh is only for an empty library.
Output directory must be new and private; files contain secrets, never commit them.
Upload JSON files into the matching Secrets Manager resources, then run sync_secrets.py.
"""
import argparse,base64,json,os,secrets,subprocess,sys
from pathlib import Path
from urllib.parse import quote,urlencode
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database-host',required=True);p.add_argument('--backup-bucket',required=True);p.add_argument('--output',required=True);p.add_argument('--fresh',action='store_true');a=p.parse_args()
for name in ['DATABASE_URL','GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET']:
    if not os.environ.get(name):p.error(name+' required')
key=os.getenv('PROVIDER_CONFIG_ENCRYPTION_KEY')
if not key:
    if not a.fresh:p.error('original encryption key required when migrating a library')
    key=base64.b64encode(secrets.token_bytes(32)).decode()
if len(base64.b64decode(key))!=32:p.error('encryption key must decode to 32 bytes')
root=Path(a.output);root.mkdir(mode=0o700,parents=True,exist_ok=False)
services=['reader','auth','ingest','pipeline','scraper','askai','cleanup','backup']
passwords={name:secrets.token_urlsafe(36) for name in services}
env={**os.environ,**{name.upper()+'_DB_PASSWORD':password for name,password in passwords.items()}}
subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/provision_db_logins.py')],check=True,env=env)
def dsn(service):return f'postgres://book_{service}_login:{quote(passwords[service])}@{a.database_host}:5432/novel_engine?'+urlencode({'sslmode':'verify-full','sslrootcert':'/etc/book/rds.pem'})
redis_password=secrets.token_urlsafe(32);redis_url=f'redis://:{redis_password}@redis:6379/0'
ingest_token=secrets.token_urlsafe(32);ask_token=secrets.token_urlsafe(32)
values={name:{'DATABASE_URL':dsn(name)} for name in services}
values['auth']={'AUTH_DATABASE_URL':dsn('auth'),'GOOGLE_CLIENT_ID':os.environ['GOOGLE_CLIENT_ID'],'GOOGLE_CLIENT_SECRET':os.environ['GOOGLE_CLIENT_SECRET']}
for name in ['reader','ingest','pipeline','scraper','cleanup']:values[name]['REDIS_URL']=redis_url
for name in ['reader','ingest','scraper']:values[name]['INGEST_INTERNAL_TOKEN']=ingest_token
for name in ['reader','askai']:values[name]['ASKAI_INTERNAL_TOKEN']=ask_token
for name in ['pipeline','askai']:values[name]['PROVIDER_CONFIG_ENCRYPTION_KEY']=key
values['ingest']['INGEST_PROVIDER_CONFIG_KEY']=key
values['backup']['BACKUP_BUCKET']=a.backup_bucket
values['cleanup']['BACKUP_BUCKET']=a.backup_bucket
values['redis']={'REDIS_PASSWORD':redis_password}
for name,value in values.items():
    path=root/(name+'.json');path.write_text(json.dumps(value,indent=2));path.chmod(0o600)
print('Generated runtime secret files in the private output directory. No secret values were printed.')
