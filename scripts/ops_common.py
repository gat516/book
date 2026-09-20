"""Portable maintenance clients. Credentials come from the process environment/IAM."""
import os
from urllib.parse import urlsplit
from minio import Minio
from minio.credentials import IamAwsProvider


def novel_prefixes(novel):
    """Both ingested/bootstrap prose and pipeline translations belong to a novel (§15.3)."""
    return (f'novels/{novel}/', f'translated/{novel}/')


def object_novel(key):
    parts = key.split('/')
    if len(parts) < 3 or parts[0] not in ('novels', 'translated') or '..' in parts:
        raise ValueError('object outside novel storage')
    return parts[1]


def objects():
    raw=os.environ['OBJECT_STORE_ENDPOINT']
    if '://' not in raw:raw=('https://' if os.getenv('OBJECT_STORE_USE_SSL')=='true' else 'http://')+raw
    endpoint=urlsplit(raw)
    key=os.environ.get('OBJECT_STORE_ACCESS_KEY')
    return Minio(endpoint.netloc,secure=endpoint.scheme=='https',
        access_key=key,secret_key=os.environ.get('OBJECT_STORE_SECRET_KEY'),
        session_token=os.environ.get('OBJECT_STORE_SESSION_TOKEN'),
        credentials=None if key else IamAwsProvider()),os.environ['OBJECT_STORE_BUCKET']
