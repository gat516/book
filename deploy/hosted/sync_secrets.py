#!/usr/bin/env python3
"""Operator/SSM host sync from Secrets Manager to the encrypted k3s secret store.

No secret is printed or written to disk. This mutates cluster Secrets and ECR pull
credentials, but does not restart services; deploy.sh controls releases explicitly.
"""
import argparse,base64,json,subprocess

def run(args,**kwargs):return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs).stdout
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prefix',default='private-books');p.add_argument('--region',default='us-east-1');p.add_argument('--registry',required=True);a=p.parse_args()
for service in ['reader','auth','ingest','pipeline','scraper','askai','cleanup','backup','redis']:
    value=run(['aws','secretsmanager','get-secret-value','--region',a.region,'--secret-id',a.prefix+'/'+service,'--query','SecretString','--output','text'])
    values=json.loads(value)
    if not isinstance(values,dict) or not all(isinstance(v,str) for v in values.values()):raise ValueError('secret must be a string-to-string JSON object')
    obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'book-'+service,'namespace':'book'},'type':'Opaque','stringData':values}
    run(['kubectl','apply','-f','-'],input=json.dumps(obj))
password=run(['aws','ecr','get-login-password','--region',a.region]).strip()
auth=base64.b64encode(('AWS:'+password).encode()).decode()
obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'ecr-pull','namespace':'book'},'type':'kubernetes.io/dockerconfigjson','stringData':{'.dockerconfigjson':json.dumps({'auths':{a.registry:{'auth':auth}}})}}
run(['kubectl','apply','-f','-'],input=json.dumps(obj))
print('Runtime secrets and registry token synchronized. Tokens need renewal before future pulls.')
