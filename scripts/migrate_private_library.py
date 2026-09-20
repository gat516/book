#!/usr/bin/env python3
"""Idempotent maintenance-window migration, after SQL migrations and before login.

Preserves book IDs, prose, chapter gates, and S3 keys. Binds credentials to their owner
and moves existing reader progress to that same explicit owner. Never chooses an owner
from a login request. Run with the migration database connection; take a backup first.
"""
import argparse
import os
import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from novel_llm.accounts import credential_aad,encryption_key,LEGACY_ACCOUNT


def migrate(db,owner_email=None):
    db.execute('SET LOCAL ROLE book_maintenance')
    db.execute('SELECT pg_advisory_xact_lock(739032)')
    if owner_email:
        email=owner_email.strip().lower()
        if '@' not in email:raise ValueError('owner email required')
        row=db.execute('SELECT email,google_subject FROM account WHERE id=%s FOR UPDATE',(LEGACY_ACCOUNT,)).fetchone()
        if not row:raise ValueError('initial library owner missing')
        if row[1] and row[0]!=email:raise ValueError('initial account already has a verified Google identity')
        db.execute('UPDATE account SET email=%s WHERE id=%s',(email,LEGACY_ACCOUNT))
        db.execute('DELETE FROM account_invitation WHERE account_id=%s AND lower(email)<>%s',(LEGACY_ACCOUNT,email))
    # Highest recorded clearance retains the solo developer's existing reading position.
    db.execute('''INSERT INTO reader_progress(reader_id,novel_id,current_chapter,updated_at)
    SELECT n.owner_id::text,p.novel_id,max(p.current_chapter),max(p.updated_at)
    FROM reader_progress p JOIN novel n ON n.id=p.novel_id GROUP BY n.owner_id,p.novel_id
    ON CONFLICT(reader_id,novel_id) DO UPDATE SET current_chapter=GREATEST(reader_progress.current_chapter,EXCLUDED.current_chapter),updated_at=GREATEST(reader_progress.updated_at,EXCLUDED.updated_at)''')
    db.execute('DELETE FROM reader_progress p USING novel n WHERE p.novel_id=n.id AND p.reader_id<>n.owner_id::text')
    rows=db.execute('SELECT account_id::text,provider,base_url,api_key_cipher,api_key_nonce FROM provider_credential WHERE key_version=0 FOR UPDATE').fetchall()
    aes=AESGCM(encryption_key()) if any(r[3] for r in rows) else None
    for account,provider,endpoint,cipher,nonce in rows:
        if cipher is None:continue
        plain=aes.decrypt(bytes(nonce),bytes(cipher),None)
        new_nonce=os.urandom(12)
        encrypted=aes.encrypt(new_nonce,plain,credential_aad(account,provider,endpoint))
        db.execute('UPDATE provider_credential SET api_key_cipher=%s,api_key_nonce=%s,key_version=1 WHERE account_id=%s AND provider=%s',(encrypted,new_nonce,account,provider))
    return len(rows)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--owner-email');args=parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        count=migrate(db,args.owner_email)
    print(f'Private library migration complete; examined {count} legacy credentials. Invite the initial owner explicitly with accounts.py.')
