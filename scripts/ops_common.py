"""Portable maintenance clients. Credentials come from the process environment/IAM."""
import os
from urllib.parse import urlsplit
from minio import Minio
from minio.credentials import IamAwsProvider


def objects():
    raw=os.environ['OBJECT_STORE_ENDPOINT']
    if '://' not in raw:raw=('https://' if os.getenv('OBJECT_STORE_USE_SSL')=='true' else 'http://')+raw
    endpoint=urlsplit(raw)
    key=os.environ.get('OBJECT_STORE_ACCESS_KEY')
    return Minio(endpoint.netloc,secure=endpoint.scheme=='https',
        access_key=key,secret_key=os.environ.get('OBJECT_STORE_SECRET_KEY'),
        credentials=None if key else IamAwsProvider()),os.environ['OBJECT_STORE_BUCKET']
