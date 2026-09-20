# Private hosted reader

This directory implements spec §15: invited Google accounts, private per-user books,
and each user's own model keys. Local development still opens directly at localhost.
The files are ready for operator configuration; no AWS deployment or invitation is
implied by their presence. [Implementation status](../../docs/HOSTED_IMPLEMENTATION.md)
records what has been tested.

The initial site is **https://qireadr.com**, with domain/DNS at Namecheap and application
hosting in AWS `us-east-1`. Google OAuth's authorized redirect URI is exactly
`https://qireadr.com/api/auth/callback`; `APP_ORIGIN` is `https://qireadr.com`.
These values are saved in the ignored `operator.env`. Export its settings in the operator
shell before following the commands below (`set -a; source deploy/hosted/operator.env;
set +a`). They do not change the local `.env` or enable login on localhost.
After AWS provisioning, point Namecheap's apex A record (Host `@`) at Terraform's
`public_ip` output. The initial release serves the apex hostname; `www` is not configured.

## Layout and operating limits

| Component | Location / purpose |
| --- | --- |
| Terraform | `terraform/`: us-east-1 VPC, EC2/k3s, RDS PostgreSQL 16, private S3, ECR, Secrets Manager, budget/alarm |
| Containers | `Dockerfile.go`, `Dockerfile.python`, `Dockerfile.web`; Rust image in `services/textproc/` |
| Kubernetes | `render.py`: reviewable manifests, TLS ingress, internal services, network policies, CronJobs |
| Secrets | `generate_secrets.py`, `sync_secrets.py`; never Terraform secret values or committed YAML |
| Schema | `db/migrate.py`, `db/hosted/`: portable baseline and forward-only checksum ledger |
| Migration / invitations | `scripts/migrate_private_library.py`, `scripts/accounts.py` |
| Recovery | `scripts/backup_private.py`, `snapshot_job.py`, `cleanup_accounts.py`, `rotate_credentials.py` |
| Tests | `db/test-account-isolation.sql`, `tests/hosted/`, shared auth/netguard tests |

The starting sizes are one `t3.large` node, one single-AZ `db.t4g.small` database,
20 GiB initial database storage, 40 GiB node disk and a separate encrypted 8 GiB Redis
EBS volume. Redis uses AOF with `everysec` and `noeviction`. Two pipeline replicas admit
one active chapter per account; scraping permits one per account and three globally.
Ask AI permits one request at a time and ten starts per minute per account.

This is one trusted application node with maintenance outages, not high availability.
The node role accesses private object storage and runtime Secrets Manager entries;
pods share that node IAM trust boundary. Application RLS enforces user isolation.
There is no public SSH, database or Kubernetes API port. Use AWS Systems Manager.
The budget notification is an alert, not a spending cap. Review a current AWS estimate
and account credits before `terraform apply`; do not assume this stack is free.

## Prerequisites and first deployment

Use an operator workstation with Terraform, AWS CLI, Docker, kubectl, PostgreSQL 16
client tools and Python dependencies (`psycopg[binary]`, `minio`, `redis`, `cryptography`,
`PyYAML`, and the local `novel-llm` package). The Python production image already has
the database clients and application dependencies. RDS is private: run DB operations
on the node or through an authenticated SSM port forward. Preserve TLS verification
and the actual RDS hostname (libpq `hostaddr` can point to the forwarded listener).

1. Choose the public domain and create a Google **web** OAuth client. Its authorized
   redirect URI is `https://DOMAIN/api/auth/callback`. Configure the consent screen/test
   users as required by that Google project. `APP_ORIGIN` is exactly `https://DOMAIN`.
2. Copy `terraform/example.tfvars` to ignored `terraform.tfvars`; set the budget email.
   Set `create_budget = false` if an existing account-wide budget already covers the
   deployment. This does not change or replace that separately managed budget.
   `us-east-1` is the default. Configure an encrypted, access-controlled Terraform state
   backend before applying, or keep initial local state in a private backed-up directory.
   Never commit state. Run `terraform init`, `terraform fmt -check`, `terraform validate`,
   then `terraform plan -out=release.tfplan`. Review the saved plan before applying it.
3. After provisioning, point the domain's A record at `public_ip`. SSM into `instance_id`;
   verify `systemctl status k3s` and the mounted `/var/lib/book-redis` volume. The node
   bootstrap installs a pinned k3s release and enables Kubernetes secret encryption.
   Configure kubectl through SSM; do not expose port 6443 publicly.
4. Build and push all six images with an immutable commit tag. Set `REGISTRY` to
   `ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/private-books` and `TAG` to the commit SHA:

   ```bash
   for service in reader-api ingest-api scraper; do
     docker build -f deploy/hosted/Dockerfile.go --build-arg SERVICE="$service" -t "$REGISTRY/$service:$TAG" .
     docker push "$REGISTRY/$service:$TAG"
   done
   docker build -f deploy/hosted/Dockerfile.python -t "$REGISTRY/python:$TAG" .
   docker build -f deploy/hosted/Dockerfile.web -t "$REGISTRY/web:$TAG" .
   docker build -f services/textproc/Dockerfile -t "$REGISTRY/textproc:$TAG" .
   for service in python web textproc; do docker push "$REGISTRY/$service:$TAG"; done
   ```

   Authenticate Docker to ECR first using `aws ecr get-login-password` piped to
   `docker login --username AWS --password-stdin REGISTRY_HOST`. CI validates builds;
   publishing and applying a release remain explicit operator steps.
5. Export the RDS operator `DATABASE_URL` from the managed master secret using a private
   shell/session. Set `sslmode=verify-full` and `sslrootcert` to `certs/rds-global.pem`.
   For an empty installation, run `python db/migrate.py --bootstrap`. For an existing
   library, follow the migration section below instead. Never bootstrap over data.
6. Export `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and the existing library's
   `PROVIDER_CONFIG_ENCRYPTION_KEY` (base64 32 bytes). Generate per-service credentials:

   ```bash
   python deploy/hosted/generate_secrets.py --database-host "$RDS_HOST" \
     --backup-bucket "$BACKUP_BUCKET" --output deploy/hosted/generated/secrets
   ```

   Only a genuinely empty library may use `--fresh` to generate a new encryption key.
   Output is mode 0600 in a new private directory. Upload each JSON file to the matching
   `private-books/SERVICE` Secrets Manager entry with `put-secret-value --secret-string
   file://PATH`. Keep the encryption key/version history separately for backup restore.
   Do not rerun generation as an ordinary deployment: it rotates database passwords.
7. Create the `book` namespace, then sync runtime secrets from an operator with AWS and
   cluster access. Pass the registry **host**, without `/private-books`, to the sync tool:

   ```bash
   kubectl create namespace book --dry-run=client -o yaml | kubectl apply -f -
   python deploy/hosted/sync_secrets.py --region us-east-1 --registry "$REGISTRY_HOST"
   python deploy/hosted/render.py --domain "$DOMAIN" --registry "$REGISTRY" --tag "$TAG" \
     --bucket "$DATA_BUCKET" --email "$TLS_EMAIL" --rds-ca deploy/hosted/certs/rds-global.pem \
     > deploy/hosted/generated/release.yaml
   bash deploy/hosted/deploy.sh deploy/hosted/generated/release.yaml
   ```

   Inspect the YAML before applying. ECR pull credentials expire after twelve hours;
   rerun secret synchronization before later image pulls or node recovery. Images are
   cached on the current node; a replacement needs fresh credentials. Custom completion
   endpoints require an explicit `PROVIDER_HEALTH_ALLOWED_HOSTS` entry in book-config.
8. Verify HTTPS, login redirects, `/api/healthz`, and the two-account isolation checklist.
   Create the initial-owner invitation only for the intended Google email:

   ```bash
   python scripts/accounts.py invite "$INITIAL_OWNER_EMAIL" --initial-owner --origin "https://$DOMAIN"
   ```

   The accepted owner is `charlesj.gatchalian@gmail.com`; it is recorded in the ignored
   operator configuration. The command prints a single-use seven-day link. It does
   **not** send email. Deliver it through the agreed email channel when available.
   Subsequent users get ordinary invitations without `--initial-owner`. First login
   never automatically claims the imported library.

## Migrating the local library

Keep a working localhost copy until the hosted smoke checks pass. Stop local application
writers for the final snapshot, retaining the existing `.env` encryption key separately.
Never copy `.env` into a Docker image or Terraform state.

For a pre-account database, first restore a `pg_dump --format=custom --no-owner --no-acl`
copy into a disposable PostgreSQL 16 instance, apply `python db/migrate.py`, and run
`python scripts/migrate_private_library.py --owner-email "$INITIAL_OWNER_EMAIL"` with
the original encryption key. Verify chapter/fact/glossary counts and key decryption.
The migration preserves book UUIDs and object names, consolidates the solo reader's
progress under the explicit owner, and upgrades legacy keys to account-bound AES-GCM.
Use a clean cluster for historical migrations; roles are cluster-global.

Once validated, `make start` performs the local schema/data upgrade during a maintenance
window and resumes the local services without Google login. It is not run implicitly by
documentation updates. The hosted import uses the upgraded portable snapshot format:

```bash
python scripts/backup_private.py create /private/path/library.tar.gz
python scripts/backup_private.py journal /private/path/deletions.json
# Point DATABASE_URL and OBJECT_STORE_* at EMPTY hosted targets, retaining original keys.
python scripts/backup_private.py restore /private/path/library.tar.gz \
  --deletion-journal /private/path/deletions.json
python db/migrate.py
python scripts/migrate_private_library.py --owner-email "$INITIAL_OWNER_EMAIL"
```

For create/journal, the database login needs `book_backup` membership; restores and
ownership/key migrations use the operator connection. `OBJECT_STORE_ENDPOINT` accepts
a URL or host:port; set `OBJECT_STORE_USE_SSL=true` for a bare AWS endpoint. Explicit
MinIO credentials work locally; absent static credentials use node IAM in AWS.
Re-provision service logins after restore: the portable snapshot does not contain login
passwords or the encryption key. Run the isolation tests before enabling traffic.
For a manual snapshot, pause the cleanup CronJob as well as application writers and
wait for any active cleanup job to finish before copying objects. The scheduled snapshot
job handles this serialization with the database advisory lock.

## Backup, erasure and restore

RDS automated backups retain seven days. At 08:00 UTC, `portable-backup` pauses ingest,
scraper and pipeline deployments, waits for them to stop, snapshots the DB and current
objects, uploads checksums and a deletion journal, and restores the original replicas.
S3 expires `snapshots/` after thirty days; `deletions/current.json` has no expiry. Cleanup
serializes with snapshot publication and removes all object versions, then repeats an
hour later. A deleted account immediately loses sessions, reads and dispatch eligibility.

Restore to a new empty database and bucket. Retrieve the snapshot **and the newest
deletion journal**, not merely a journal from the snapshot date. Supply the matching
encryption key versions. The restore verifies hashes, reinstates database grants,
replays erasures, copies only active owners' objects, and revokes every old session.
Start with a fresh Redis queue; worker startup reconciles durable unfinished jobs.
Test private book reads, chapter gates, and provider-key decryption before switching DNS.

If a hard node failure leaves writers scaled to zero after backup, verify that no backup
job is active, then restore ingest-api=1, scraper=1, pipeline=2. Reapply the rendered release
to restore the full desired state. During a failed deploy, migrations remain forward-only;
fix forward or restore a tested snapshot into empty targets. Never roll back SQL files.

Check `kubectl -n book get cronjobs,jobs,pods`, recent job logs, latest snapshot timestamps,
Redis volume free space, RDS storage and the AWS budget. The EC2 alarm is available in
CloudWatch; notification actions must be configured by the operator. Alert on missed
daily snapshots and rehearse restoration after every schema/permission change.

## Key rotation and account operations

Stop writers/workers. Export the current `PROVIDER_CONFIG_ENCRYPTION_KEY[_Vn]` keys plus
`ROTATION_NEW_KEY` and a strictly higher `ROTATION_NEW_VERSION`, then run
`python scripts/rotate_credentials.py` through the operator connection. It decrypts and
re-encrypts all saved credentials in one transaction using new random nonces.
Update the ingest encryption key and Python encryption key together, set
`PROVIDER_CONFIG_KEY_VERSION` to the new version, sync Secrets Manager, and restart the
services before accepting writes. Retain old key versions for the full backup window.

`python scripts/accounts.py disable EMAIL` revokes sessions and dispatch eligibility.
The web account-deletion action requests durable erasure. Operators should not delete
rows directly as a substitute for that workflow: the journal prevents resurrection
from older backups. Local mode deliberately has no account-delete/login UI.

## Validation

`tests/hosted/README.md` contains exact disposable test commands. Required checks include
the SQL ownership/spoiler tests, real restricted-role HTTP smoke, full versioned-object
deletion/restore, shared auth tests and UI mode tests. No paid model requests are needed.
After configuration, additionally verify real Google consent/callback, DNS/TLS issuance,
RDS connectivity, node IAM, scheduled snapshots, and a second invited account in AWS.
