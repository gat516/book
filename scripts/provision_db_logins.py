#!/usr/bin/env python3
"""Create/update least-privilege service logins. Passwords supplied in environment.

Run as database operator after bootstrap. No secret is printed. Store each resulting
connection string in its own service secret. Does not create a shared admin app login.
"""
import os
import psycopg
from psycopg import sql

ROLES={'reader':['rls_reader','reader_progress_writer'],'auth':['book_auth'],'ingest':['ingest_writer'],
       'pipeline':['book_worker'],'scraper':['book_worker'],'askai':['rls_reader'],'cleanup':['book_cleanup'],'backup':['book_backup']}
with psycopg.connect(os.environ['DATABASE_URL']) as db:
    for service,roles in ROLES.items():
        name='book_'+service+'_login';password=os.environ[service.upper()+'_DB_PASSWORD']
        if len(password)<24:raise ValueError('use generated passwords of at least 24 characters')
        if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(name,)).fetchone():db.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT').format(sql.Identifier(name)))
        db.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(name),sql.Literal(password)))
        for role in roles:db.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(role),sql.Identifier(name)))
        db.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(db.info.dbname),sql.Identifier(name)))
print('Service logins provisioned.')
