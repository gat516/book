#!/usr/bin/env python3
"""Operator-only account lifecycle. Run via with-env and the pipeline virtualenv.

DATABASE_URL must be a migration/operator connection, never an HTTP-facing role.
No email is sent. Invite links are printed once for manual delivery.
"""
import argparse, hashlib, os, secrets
import psycopg

LEGACY = '00000000-0000-4000-8000-000000000001'
def main():
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest='command', required=True)
    invite=commands.add_parser('invite');invite.add_argument('email');invite.add_argument('--initial-owner',action='store_true')
    invite.add_argument('--origin',default=os.environ.get('APP_ORIGIN'))
    disable=commands.add_parser('disable');disable.add_argument('email')
    args=p.parse_args()
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        db.execute('SET LOCAL ROLE book_auth')
        if args.command=='invite':
            if not args.origin or not args.origin.startswith('https://'):p.error('--origin must be an HTTPS website origin')
            token=secrets.token_urlsafe(32)
            email=args.email.strip().lower()
            if '@' not in email:p.error('email required')
            if args.initial_owner:
                row=db.execute('SELECT google_subject FROM account WHERE id=%s FOR UPDATE',(LEGACY,)).fetchone()
                if not row or row[0]:p.error('initial account is already claimed')
                # One active initial-owner invitation; never auto-claim legacy data on signup.
                db.execute('DELETE FROM account_invitation WHERE account_id=%s',(LEGACY,))
            db.execute("INSERT INTO account_invitation(token_hash,email,expires_at,account_id) VALUES(%s,%s,now()+interval '7 days',%s)",(hashlib.sha256(token.encode()).hexdigest(),email,LEGACY if args.initial_owner else None))
            db.commit()
            print(args.origin.rstrip('/')+'/?invite='+token)
        else:
            row=db.execute("UPDATE account SET status='disabled' WHERE lower(email)=%s RETURNING id",(args.email.lower(),)).fetchone()
            if not row:p.error('account not found')
            db.execute('DELETE FROM account_session WHERE account_id=%s',(row[0],))
            print('Account disabled; sessions revoked.')
if __name__=='__main__':main()
