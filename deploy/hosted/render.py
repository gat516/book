#!/usr/bin/env python3
"""Render reviewable Kubernetes YAML; never connects to AWS or applies resources.

Production secrets are separate Secret objects populated by sync_secrets.py. Every
application image must use a commit tag (ECR is immutable), never latest.
"""
import argparse,json,re
from pathlib import Path
import yaml


def render(domain,registry,tag,bucket,region,email,ca,maintenance_tag=None):
    if not re.fullmatch(r'[a-zA-Z0-9.-]+',domain) or '.' not in domain:raise ValueError('public domain required')
    if tag in {'latest','main','master',''}:raise ValueError('immutable release tag required')
    if maintenance_tag in {'latest','main','master',''}:raise ValueError('immutable maintenance tag required')
    ns='book';resources=[]
    def add(kind,name,spec=None,api='v1',**rest):
        obj={'apiVersion':api,'kind':kind,'metadata':{'name':name,'namespace':ns},**rest}
        if spec is not None:obj['spec']=spec
        resources.append(obj);return obj
    resources.append({'apiVersion':'v1','kind':'Namespace','metadata':{'name':ns,'labels':{'pod-security.kubernetes.io/enforce':'baseline'}}})
    add('ConfigMap','book-config',data={
        'BOOK_MODE':'hosted','APP_ORIGIN':'https://'+domain,'AWS_REGION':region,'AWS_DEFAULT_REGION':region,
        'OBJECT_STORE_ENDPOINT':f's3.{region}.amazonaws.com','OBJECT_STORE_USE_SSL':'true','OBJECT_STORE_BUCKET':bucket,
        'INGEST_API_URL':'http://ingest-api:8080','ASKAI_URL':'http://askai:8082','SCRAPER_URL':'http://scraper:8083',
        'TEXTPROC_BACKEND':'grpc','TEXTPROC_GRPC_ADDR':'textproc:50051','EMBED_PROVIDER':'auto',
        'LLM_PROVIDER':'gemini','LLM_MODEL_ASK':'gemini-2.5-flash','LLM_MODEL_EXTRACT':'gemini-2.5-flash',
        'LLM_MODEL_TRANSLATE':'gemini-2.5-flash','PROVIDER_HEALTH_ALLOWED_HOSTS':'',
        'SCRAPE_MAX_CONCURRENT_JOBS':'3','SCRAPE_MAX_QUEUE_DEPTH':'50','PROVIDER_CONFIG_KEY_VERSION':'1'})
    add('ConfigMap','rds-ca',data={'rds.pem':ca})
    security={'runAsNonRoot':True,'runAsUser':10001,'runAsGroup':10001,'fsGroup':10001,'seccompProfile':{'type':'RuntimeDefault'}}
    def pod(name,image,secret,command=None,port=None,memory='512Mi'):
        container={'name':name,'image':f'{registry}/{image}:{tag}','imagePullPolicy':'IfNotPresent',
            'envFrom':[{'configMapRef':{'name':'book-config'}}]+([{'secretRef':{'name':'book-'+secret}}] if secret else []),
            'resources':{'requests':{'cpu':'100m','memory':'128Mi'},'limits':{'cpu':'1','memory':memory}},
            'securityContext':{'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
            'volumeMounts':[{'name':'tmp','mountPath':'/tmp'},{'name':'rds-ca','mountPath':'/etc/book','readOnly':True}]}
        if command:container['command']=command
        if port:
            container['ports']=[{'containerPort':port}]
            probe={'httpGet':{'path':'/healthz','port':port},'periodSeconds':10,'timeoutSeconds':5}
            if name=='textproc':probe={'tcpSocket':{'port':port},'periodSeconds':10}
            container['readinessProbe']=probe
            container['startupProbe']={**probe,'failureThreshold':30}
        if name=='reader-api':container['envFrom'].append({'secretRef':{'name':'book-auth'}})
        if name=='web':security_override={**security,'runAsUser':101,'runAsGroup':101,'fsGroup':101}
        else:security_override=security
        # §15: explicit service DNS/config only; Kubernetes' ASKAI_PORT is a tcp:// URL.
        return {'enableServiceLinks':False,'automountServiceAccountToken':False,'imagePullSecrets':[{'name':'ecr-pull'}],
            'terminationGracePeriodSeconds':90,'securityContext':security_override,
            'containers':[container],'volumes':[{'name':'tmp','emptyDir':{}},{'name':'rds-ca','configMap':{'name':'rds-ca'}}]}
    for name,image,secret,command,port,replicas,memory in [
        ('reader-api','reader-api','reader',None,8081,1,'384Mi'),
        ('ingest-api','ingest-api','ingest',None,8080,1,'384Mi'),
        ('scraper','scraper','scraper',None,8083,1,'384Mi'),
        ('pipeline','python','pipeline',['python','-m','pipeline.worker'],None,2,'1536Mi'),
        ('askai','python','askai',['python','-m','askai'],8082,1,'1024Mi'),
        ('textproc','textproc',None,None,50051,1,'384Mi'),
        ('web','web',None,None,8080,1,'128Mi')]:
        labels={'app':name}
        spec=pod(name,image,secret,command,port,memory)
        add('Deployment',name,{'replicas':replicas,'strategy':{'type':'Recreate'},'selector':{'matchLabels':labels},'template':{'metadata':{'labels':labels},'spec':spec}},api='apps/v1')
        if port:add('Service',name,{'selector':labels,'ports':[{'port':port,'targetPort':port}]})
    # Single-node Redis data lives on a separately attached encrypted EBS volume.
    resources.append({'apiVersion':'v1','kind':'PersistentVolume','metadata':{'name':'book-redis'},'spec':{
        'capacity':{'storage':'8Gi'},'accessModes':['ReadWriteOnce'],'persistentVolumeReclaimPolicy':'Retain',
        'storageClassName':'','local':{'path':'/var/lib/book-redis'},
        'nodeAffinity':{'required':{'nodeSelectorTerms':[{'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':['book-node']}]}]}}}})
    add('PersistentVolumeClaim','redis-data',{'accessModes':['ReadWriteOnce'],'storageClassName':'','volumeName':'book-redis','resources':{'requests':{'storage':'8Gi'}}})
    add('Deployment','redis',{'replicas':1,'strategy':{'type':'Recreate'},'selector':{'matchLabels':{'app':'redis'}},'template':{'metadata':{'labels':{'app':'redis'}},'spec':{
        'enableServiceLinks':False,'automountServiceAccountToken':False,'securityContext':{'runAsUser':999,'runAsGroup':999,'fsGroup':999,'runAsNonRoot':True},
        'containers':[{'name':'redis','image':'redis:7.4-alpine','command':['redis-server'],'args':['--appendonly','yes','--appendfsync','everysec','--maxmemory','1gb','--maxmemory-policy','noeviction','--requirepass','$(REDIS_PASSWORD)'],
            'envFrom':[{'secretRef':{'name':'book-redis'}}],'env':[{'name':'REDISCLI_AUTH','valueFrom':{'secretKeyRef':{'name':'book-redis','key':'REDIS_PASSWORD'}}}],
            'ports':[{'containerPort':6379}],'readinessProbe':{'exec':{'command':['redis-cli','ping']},'periodSeconds':10},
            'resources':{'requests':{'cpu':'100m','memory':'256Mi'},'limits':{'cpu':'500m','memory':'1280Mi'}},
            'securityContext':{'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}},
            'volumeMounts':[{'name':'data','mountPath':'/data'}]}],'volumes':[{'name':'data','persistentVolumeClaim':{'claimName':'redis-data'}}]}}},api='apps/v1')
    add('Service','redis',{'selector':{'app':'redis'},'ports':[{'port':6379}]})
    cleanup=pod('cleanup','python','cleanup',['python','/app/scripts/cleanup_accounts.py'])
    if maintenance_tag:cleanup['containers'][0]['image']=f'{registry}/python:{maintenance_tag}'
    cleanup['restartPolicy']='OnFailure'
    add('CronJob','account-cleanup',{'suspend':False,'schedule':'*/5 * * * *','concurrencyPolicy':'Forbid','successfulJobsHistoryLimit':1,'failedJobsHistoryLimit':3,'jobTemplate':{'spec':{'backoffLimit':3,'activeDeadlineSeconds':240,'template':{'metadata':{'labels':{'app':'cleanup'}},'spec':cleanup}}}},api='batch/v1')
    backup=pod('backup','python','backup',['python','/app/scripts/snapshot_job.py'],memory='1024Mi')
    if maintenance_tag:backup['containers'][0]['image']=f'{registry}/python:{maintenance_tag}'
    backup['automountServiceAccountToken']=True
    backup['serviceAccountName']='snapshot'
    backup['restartPolicy']='Never'
    add('ServiceAccount','snapshot')
    add('Role','snapshot',api='rbac.authorization.k8s.io/v1',rules=[
        {'apiGroups':['apps'],'resources':['deployments/scale'],'resourceNames':['ingest-api','scraper','pipeline'],'verbs':['get','patch']},
        {'apiGroups':[''],'resources':['pods'],'verbs':['list']}])
    add('RoleBinding','snapshot',api='rbac.authorization.k8s.io/v1',roleRef={'apiGroup':'rbac.authorization.k8s.io','kind':'Role','name':'snapshot'},subjects=[{'kind':'ServiceAccount','name':'snapshot','namespace':'book'}])
    add('CronJob','portable-backup',{'suspend':False,'schedule':'0 8 * * *','timeZone':'Etc/UTC','concurrencyPolicy':'Forbid',
        'successfulJobsHistoryLimit':2,'failedJobsHistoryLimit':3,'jobTemplate':{'spec':{'backoffLimit':0,'activeDeadlineSeconds':1800,
        'template':{'metadata':{'labels':{'app':'backup'}},'spec':backup}}}},api='batch/v1')
    # Private services accept traffic only from the caller that needs them.
    allow={'web':[('kube-system',None)],'reader-api':[(ns,'web')],'ingest-api':[(ns,'reader-api'),(ns,'scraper')],
           'askai':[(ns,'reader-api')],'scraper':[(ns,'reader-api')],'textproc':[(ns,'pipeline')],
           'redis':[(ns,x) for x in ['reader-api','ingest-api','pipeline','scraper','cleanup']]}
    ports={'web':8080,'reader-api':8081,'ingest-api':8080,'askai':8082,'scraper':8083,'textproc':50051,'redis':6379}
    for app,callers in allow.items():
        sources=[]
        for namespace,caller in callers:
            source={'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}}}
            if caller:source['podSelector']={'matchLabels':{'app':caller}}
            sources.append(source)
        add('NetworkPolicy','allow-'+app,{'podSelector':{'matchLabels':{'app':app}},'policyTypes':['Ingress'],'ingress':[{'from':sources,'ports':[{'port':ports[app],'protocol':'TCP'}]}]},api='networking.k8s.io/v1')
    add('Ingress','book',{'ingressClassName':'traefik','tls':[{'hosts':[domain]}],'rules':[{'host':domain,'http':{'paths':[{'path':'/','pathType':'Prefix','backend':{'service':{'name':'web','port':{'number':8080}}}}]}}]},api='networking.k8s.io/v1')
    resources[-1]['metadata']['annotations']={'traefik.ingress.kubernetes.io/router.entrypoints':'websecure','traefik.ingress.kubernetes.io/router.tls':'true','traefik.ingress.kubernetes.io/router.tls.certresolver':'letsencrypt'}
    resources.append({'apiVersion':'helm.cattle.io/v1','kind':'HelmChartConfig','metadata':{'name':'traefik','namespace':'kube-system'},'spec':{'valuesContent':yaml.safe_dump({
        # §15: clients use public 443; the chart's websecure listener is internal 8443.
        'additionalArguments':[f'--certificatesresolvers.letsencrypt.acme.email={email}','--certificatesresolvers.letsencrypt.acme.storage=/data/acme.json','--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web','--entrypoints.web.http.redirections.entrypoint.to=:443','--entrypoints.web.http.redirections.entrypoint.scheme=https'],
        # §15 accepts ingress maintenance: ACME state/challenges belong to one process.
        'updateStrategy':{'type':'Recreate'},
        'persistence':{'enabled':True,'size':'1Gi','storageClass':'local-path'}})}})
    return resources

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['domain','registry','tag','bucket','email','rds-ca']:p.add_argument('--'+name,required=True)
    p.add_argument('--maintenance-tag',help='optional immutable Python tag for backup and cleanup only')
    p.add_argument('--region',default='us-east-1');a=p.parse_args()
    print(yaml.safe_dump_all(render(a.domain,a.registry,a.tag,a.bucket,a.region,a.email,Path(a.rds_ca).read_text(),a.maintenance_tag),sort_keys=False))
