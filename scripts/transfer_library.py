#!/usr/bin/env python3
"""Copy a private library into an existing invited account (§15.1–15.3).

Export while local writers are stopped. Import uses an operator connection and
requires an empty destination library and keys. Authentication tables are never
copied. The private archive contains ciphertext, never encryption keys. Supply the
original key as TRANSFER_SOURCE_KEY, separately from the target runtime key.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from novel_llm.accounts import credential_aad, encryption_key, LEGACY_ACCOUNT
from ops_common import objects, novel_prefixes, object_novel

# FK order; source-chapter values, glossary audit chains and prose are unchanged (§0).
TABLES = (
    'novel', 'novel_provider_config', 'chapter', 'chapter_translation_version',
    'chapter_fact', 'fact_retraction', 'subject', 'wiki_page', 'chunk',
    'glossary', 'glossary_changelog', 'glossary_candidate', 'glossary_candidate_chapter',
    'character_name_checkpoint', 'character_name_review', 'character_name_occurrence',
    'term_rendering_occurrence', 'mention_span', 'chapter_failure', 'job', 'scrape_job',
    'reader_progress', 'provider_credential', 'embedding_config',
)
ACCOUNT_TABLES = {'provider_credential', 'embedding_config'}


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ledger(db):
    # The original local migration runner recorded versions only. Do not rewrite
    # that live ledger during a copy or pretend its checksums were recorded (§15.3).
    has_checksum = db.execute("SELECT 1 FROM information_schema.columns WHERE table_name='schema_migrations' AND column_name='checksum'").fetchone()
    checksum = 'checksum' if has_checksum else 'NULL::text'
    return db.execute(sql.SQL('SELECT version,{} FROM schema_migrations ORDER BY version').format(sql.SQL(checksum))).fetchall()


def compatible_ledgers(source, target, source_only=()):
    source = [r for r in source if r[0] not in source_only]
    if [r[0] for r in source] != [r[0] for r in target]:
        return False
    return all(old[1] is None or old[1] == new[1] for old, new in zip(source, target))


def prepare_rows(table, rows, target, source_key=None, target_key=None):
    """Transform only ownership, credential envelopes and nonportable work claims."""
    result = [dict(row) for row in rows]
    if table == 'reader_progress':
        by_novel = {}
        for row in result:
            old = by_novel.get(row['novel_id'])
            if old is None or row['current_chapter'] > old['current_chapter']:
                by_novel[row['novel_id']] = {**row, 'reader_id': target}
        return list(by_novel.values())
    for row in result:
        if table == 'novel':
            row['owner_id'] = target
        elif table in ACCOUNT_TABLES:
            source = row['account_id']
            row['account_id'] = target
            if table == 'provider_credential' and row['api_key_cipher']:
                # §15.2: account + provider + endpoint remain cryptographically bound.
                if row['key_version'] not in (0, 1):
                    raise ValueError('source credential key version needs an explicit migration')
                aad = credential_aad(source, row['provider'], row['base_url']) if row['key_version'] else None
                plain = AESGCM(source_key).decrypt(bytes.fromhex(row['api_key_nonce'][2:]), bytes.fromhex(row['api_key_cipher'][2:]), aad)
                nonce = os.urandom(12)
                encrypted = AESGCM(target_key).encrypt(nonce, plain, credential_aad(target, row['provider'], row['base_url']))
                row.update(api_key_nonce='\\x' + nonce.hex(), api_key_cipher='\\x' + encrypted.hex(), key_version=1)
        elif table == 'scrape_job' and row['status'] in ('pending', 'running'):
            row.update(status='cancelled', cancel_requested=True, last_error='Local import: resume fetching explicitly from the reader.')
        elif table == 'job' and row['state'] in ('processing', 'running', 'submitted'):
            row.update(state='pending', batch_id=None)
    return result


def export_library(output, owner):
    client, bucket = objects()
    with psycopg.connect(os.environ['DATABASE_URL']) as db, tempfile.TemporaryDirectory(prefix='book-transfer-') as tmp:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        db.execute('SET LOCAL ROLE book_backup')
        novel_ids = [str(row[0]) for row in db.execute("SELECT n.id FROM novel n JOIN account a ON n.owner_id=a.id WHERE n.owner_id=%s AND a.status='active' ORDER BY n.id", (owner,))]
        if not novel_ids:
            raise ValueError('source library is empty')
        work = Path(tmp)
        (work / 'objects').mkdir()
        data = {'format': 1, 'owner': owner, 'novels': novel_ids, 'ledger': ledger(db), 'tables': {}, 'objects': []}
        for table in TABLES:
            if table == 'novel':
                where, args = 'owner_id=%s', (owner,)
            elif table in ACCOUNT_TABLES:
                where, args = 'account_id=%s', (owner,)
            else:
                where, args = 'novel_id=ANY(%s::uuid[])', (novel_ids,)
            query = sql.SQL('SELECT to_jsonb(t) FROM {} t WHERE ' + where).format(sql.Identifier(table))
            data['tables'][table] = [r[0] for r in db.execute(query, args)]
        for prefix in [p for novel in novel_ids for p in novel_prefixes(novel)]:
            for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
                filename = hashlib.sha256(obj.object_name.encode()).hexdigest()
                path = work / 'objects' / filename
                client.fget_object(bucket, obj.object_name, str(path), version_id=obj.version_id)
                data['objects'].append({'key': obj.object_name, 'file': filename, 'sha256': digest(path)})
        object_keys = {o['key'] for o in data['objects']}
        for table in ('chapter', 'chapter_translation_version'):
            for row in data['tables'][table]:
                for field in ('raw_uri', 'translated_uri'):
                    if row.get(field) and row[field] not in object_keys:
                        raise ValueError(f'{table} references a missing object')
        (work / 'manifest.json').write_text(json.dumps(data))
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream, tarfile.open(fileobj=stream, mode='w:gz') as archive:
            archive.add(work / 'manifest.json', arcname='manifest.json')
            archive.add(work / 'objects', arcname='objects')
    return {'novels': len(novel_ids), 'tables': {t: len(r) for t, r in data['tables'].items()}, 'objects': len(data['objects']), 'sha256': digest(output)}


def validate_bundle(work, data):
    if data.get('format') != 1 or set(data.get('tables', {})) != set(TABLES):
        raise ValueError('unsupported library transfer')
    novels = set(data['novels'])
    if not novels or novels != {r['id'] for r in data['tables']['novel']}:
        raise ValueError('novel manifest mismatch')
    for table, rows in data['tables'].items():
        for row in rows:
            if table == 'novel' and row['owner_id'] != data['owner']:
                raise ValueError('foreign owner in transfer')
            if table in ACCOUNT_TABLES and row['account_id'] != data['owner']:
                raise ValueError('foreign credentials/settings in transfer')
            if table not in ACCOUNT_TABLES and table != 'novel' and row['novel_id'] not in novels:
                raise ValueError('foreign novel in transfer')
    seen = set()
    for obj in data['objects']:
        if object_novel(obj['key']) not in novels:
            raise ValueError('object outside the source library')
        if obj['file'] != hashlib.sha256(obj['key'].encode()).hexdigest() or obj['key'] in seen:
            raise ValueError('invalid object filename or duplicate')
        seen.add(obj['key'])
        if digest(work / 'objects' / obj['file']) != obj['sha256']:
            raise ValueError('object checksum mismatch')
    for table in ('chapter', 'chapter_translation_version'):
        for row in data['tables'][table]:
            for field in ('raw_uri', 'translated_uri'):
                if row.get(field) and row[field] not in seen:
                    raise ValueError('missing referenced prose')


def import_library(archive, email, apply=False, source_only_migrations=()):
    import base64
    client, bucket = objects()
    with tempfile.TemporaryDirectory(prefix='book-transfer-import-') as tmp:
        work = Path(tmp)
        with tarfile.open(archive) as tar:
            # Trusted operator archive; reject links as well as path traversal.
            if any(not (m.isfile() or m.isdir()) or m.name.startswith('/') or '..' in m.name.split('/') for m in tar.getmembers()):
                raise ValueError('unsafe archive member')
            tar.extractall(work, filter='data')
        data = json.loads((work / 'manifest.json').read_text())
        validate_bundle(work, data)
        source_key = base64.b64decode(os.environ['TRANSFER_SOURCE_KEY'], validate=True)
        target_key = encryption_key()
        if len(source_key) != 32:
            raise ValueError('invalid source encryption key')
        with psycopg.connect(os.environ['DATABASE_URL']) as db:
            db.execute('SET LOCAL ROLE book_maintenance')
            db.execute('SELECT pg_advisory_xact_lock(739032)')
            target_ledger = ledger(db)
            # §15.3: an operator may name a historical source-only ledger label;
            # this never permits checksum drift or missing target migrations.
            extra = {r[0] for r in data['ledger']} - {r[0] for r in target_ledger}
            if set(source_only_migrations) - extra:
                raise ValueError('migration exception must name a source-only ledger entry')
            if not compatible_ledgers(data['ledger'], target_ledger, source_only_migrations):
                raise ValueError('source and target migration ledgers differ')
            row = db.execute("SELECT id::text,google_subject FROM account WHERE lower(email)=%s AND status='active' FOR UPDATE", (email.strip().lower(),)).fetchone()
            if not row or not row[1]:
                raise ValueError('target must be an active, verified Google account')
            target = row[0]
            for table, column in [('novel', 'owner_id'), ('provider_credential', 'account_id'), ('embedding_config', 'account_id')]:
                if db.execute(sql.SQL('SELECT count(*) FROM {} WHERE {}=%s').format(sql.Identifier(table), sql.Identifier(column)), (target,)).fetchone()[0]:
                    raise ValueError('target library/settings must be empty; existing data will not be overwritten')
            if db.execute('SELECT 1 FROM novel WHERE id=ANY(%s::uuid[])', (data['novels'],)).fetchone():
                raise ValueError('novel ID already exists in target database')
            if db.execute('SELECT 1 FROM account_cleanup WHERE novel_id=ANY(%s::uuid[])', (data['novels'],)).fetchone():
                raise ValueError('cannot import a novel scheduled for deletion')
            if db.execute('SELECT 1 FROM account_deletion_tombstone WHERE account_id=%s', (target,)).fetchone():
                raise ValueError('cannot import into a deleted account')
            prepared = {t: prepare_rows(t, rows, target, source_key, target_key) for t, rows in data['tables'].items()}
            # Check every table's complete schema before any object or row is written.
            columns = {}
            for table, rows in prepared.items():
                columns[table] = [r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table,))]
                if any(set(r) != set(columns[table]) for r in rows):
                    raise ValueError(f'{table} column mismatch')
            expected = {o['key']: o for o in data['objects']}
            for prefix in [p for novel in data['novels'] for p in novel_prefixes(novel)]:
                for obj in client.list_objects(bucket, prefix=prefix, recursive=True):
                    if obj.object_name not in expected:
                        raise ValueError('target contains unexpected novel objects')
                    response = client.get_object(bucket, obj.object_name)
                    try:
                        if hashlib.sha256(response.read()).hexdigest() != expected[obj.object_name]['sha256']:
                            raise ValueError('target object conflict')
                    finally:
                        response.close(); response.release_conn()
            report = {'target': email, 'account_id': target, 'novels': data['novels'], 'tables': {t: len(r) for t, r in prepared.items()}, 'objects': len(data['objects']), 'source_checksums_recorded': all(r[1] for r in data['ledger']), 'applied': apply}
            if not apply:
                return report
            # §15: retain the destination key/identity; only the authorized library moves.
            # Objects precede the DB commit. An interrupted copy can be retried after
            # hashes are checked; no database row can point to an absent object.
            for obj in data['objects']:
                client.fput_object(bucket, obj['key'], str(work / 'objects' / obj['file']))
                response = client.get_object(bucket, obj['key'])
                try:
                    if hashlib.sha256(response.read()).hexdigest() != obj['sha256']:
                        raise ValueError('uploaded object checksum mismatch')
                finally:
                    response.close(); response.release_conn()
            for table in TABLES:
                rows = prepared[table]
                if not rows:
                    continue
                cols = sql.SQL(',').join(map(sql.Identifier, columns[table]))
                db.execute(sql.SQL('INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM jsonb_populate_recordset(NULL::{}, %s)').format(sql.Identifier(table), cols, cols, sql.Identifier(table)), (Jsonb(rows),))
                # Imported serial IDs must not collide with future inserts.
                for col in columns[table]:
                    sequence = db.execute('SELECT pg_get_serial_sequence(%s,%s)', (table, col)).fetchone()[0]
                    if sequence:
                        db.execute(sql.SQL('SELECT setval(%s, GREATEST((SELECT COALESCE(max({}),1) FROM {}),(SELECT last_value FROM {})),true)').format(sql.Identifier(col), sql.Identifier(table), sql.Identifier(*sequence.split('.'))), (sequence,))
            db.execute("INSERT INTO account_queue_control(account_id,mode) VALUES(%s,'paused') ON CONFLICT(account_id) DO UPDATE SET mode='paused',focus_novel_id=NULL,updated_at=now()", (target,))
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export'); export.add_argument('archive'); export.add_argument('--owner', default=LEGACY_ACCOUNT)
    imp = sub.add_parser('import'); imp.add_argument('archive'); imp.add_argument('--email', required=True); imp.add_argument('--apply', action='store_true')
    imp.add_argument('--source-only-migration', action='append', default=[], help='explicit historical source-only version label; never bypasses recorded checksums')
    args = parser.parse_args()
    result = export_library(args.archive, args.owner) if args.command == 'export' else import_library(args.archive, args.email, args.apply, args.source_only_migration)
    print(json.dumps(result, indent=2))
