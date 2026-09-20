#!/usr/bin/env python3
"""Offline, transactional key rotation; never prints keys. Stop writers/workers first.

Set ROTATION_NEW_KEY (base64 32 bytes), ROTATION_NEW_VERSION (monotonic integer),
plus current PROVIDER_CONFIG_ENCRYPTION_KEY[_Vn]. After success update all service
secrets together, retaining old versions for backup restore, then restart services.
"""
import base64,os
import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from novel_llm.accounts import encryption_key,credential_aad

def main():
    version=int(os.environ['ROTATION_NEW_VERSION']);new=AESGCM(base64.b64decode(os.environ['ROTATION_NEW_KEY']))
    if version<2:raise ValueError('new version must be at least two')
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        db.execute('SET LOCAL ROLE book_maintenance')
        db.execute('SELECT pg_advisory_xact_lock(739032)')
        for account,provider,endpoint,cipher,nonce,old in db.execute('SELECT account_id::text,provider,base_url,api_key_cipher,api_key_nonce,key_version FROM provider_credential FOR UPDATE').fetchall():
            if cipher is None:continue
            if old>=version:raise ValueError('new version must exceed every stored version')
            aad=credential_aad(account,provider,endpoint)
            plain=AESGCM(encryption_key(old or 1)).decrypt(bytes(nonce),bytes(cipher),aad if old else None)
            nonce=os.urandom(12)
            db.execute('UPDATE provider_credential SET api_key_cipher=%s,api_key_nonce=%s,key_version=%s,updated_at=now() WHERE account_id=%s AND provider=%s',(new.encrypt(nonce,plain,aad),nonce,version,account,provider))
    print('Rotation committed. Update service secrets and restart before accepting writes.')
if __name__=='__main__':main()
