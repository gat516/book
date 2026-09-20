#!/usr/bin/env python3
"""Atomic, checksum-verified migrations with one database advisory lock.

--bootstrap installs the portable hosted baseline into an EMPTY database. Existing
installations use the historical ledger and only apply new files. Requires psycopg.
"""
import argparse,hashlib,json,os,re,sys
from pathlib import Path
import psycopg

ROOT=Path(__file__).resolve().parent

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def transactional_sql(path):
    # Historical files own an outer transaction; the runner now owns it with the ledger.
    return re.sub(r'(?mi)^\s*(BEGIN|COMMIT);\s*$', '', path.read_text())

def migrate(db,bootstrap=False):
    db.execute('SELECT pg_advisory_lock(739030)')
    try:
        if bootstrap:
            if db.execute("SELECT to_regclass('public.novel')").fetchone()[0]:
                raise RuntimeError('bootstrap requires an empty database')
            manifest=json.loads((ROOT/'hosted/baseline.json').read_text())
            for name,checksum in manifest.items():
                if digest(ROOT/'migrations'/name)!=checksum:raise RuntimeError(f'baseline migration drift: {name}')
            with db.transaction():
                db.execute((ROOT/'hosted/roles.sql').read_text())
                db.execute((ROOT/'hosted/baseline.sql').read_text())
                db.execute("SET search_path=public; SET row_security=on")
                db.execute("INSERT INTO account(id,status) VALUES('00000000-0000-4000-8000-000000000001','active')")
                db.execute('CREATE TABLE schema_migrations(version text PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT now(),checksum text)')
                for name,checksum in manifest.items():db.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)',(name,checksum))
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT now(),checksum text)')
        db.execute('ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text')
        for path in sorted((ROOT/'migrations').glob('*.sql')):
            checksum=digest(path)
            row=db.execute('SELECT checksum FROM schema_migrations WHERE version=%s',(path.name,)).fetchone()
            if row:
                if row[0] and row[0]!=checksum:raise RuntimeError(f'applied migration changed: {path.name}')
                if not row[0]:db.execute('UPDATE schema_migrations SET checksum=%s WHERE version=%s',(checksum,path.name))
                continue
            with db.transaction():
                db.execute(transactional_sql(path))
                db.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)',(path.name,checksum))
            print('applied',path.name)
    finally:db.execute('SELECT pg_advisory_unlock(739030)')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bootstrap',action='store_true')
    parser.add_argument('--check',action='store_true',help='read-only; exit 3 when migrations are pending')
    args=parser.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True) as db:
        if args.check:
            exists=db.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
            applied={row[0] for row in db.execute('SELECT version FROM schema_migrations')} if exists else set()
            pending=[p.name for p in sorted((ROOT/'migrations').glob('*.sql')) if p.name not in applied]
            if pending:print('pending:',', '.join(pending));sys.exit(3)
        else:migrate(db,args.bootstrap)
    print('migrations up to date')
