# Automatic application releases

Push to `book/main` → `checks` passes → `Release` builds six immutable ECR
images → SSM updates AWS → Cloudflare publishes the frontend → live verification.
A newer main commit supersedes an older release before deployment. Releases run
one at a time and are not cancelled by subsequent pushes.

AWS releases update reader-api, ingest-api, scraper, pipeline, askai, textproc,
the origin web server, and both maintenance CronJob images. Redis, the optional
gateway, database, secrets, networking and infrastructure are retained. The
gateway belongs to its own repository. Database-file changes fail before deploying;
apply and verify migrations manually, then update `approved_db_tree` in config.json
and the SSM document. CD never attempts to reverse SQL migrations.

## One-time connection

From an AWS operator session:

```bash
AWS_PROFILE=book python3 deploy/hosted/cd/configure.py --apply
gh variable set AWS_DEPLOY_ROLE_ARN --repo gat516/book \
  --body arn:aws:iam::964862484671:role/qireadr-github-release
gh variable set CLOUDFLARE_ACCOUNT_ID --repo gat516/book \
  --body 9ab4e12bab6cac614015a3f133769e27
```

Create a Cloudflare API token for this account and qireadr.com with Account /
Workers Scripts / Edit, Account / Account Settings / Read, Zone / Workers Routes /
Edit, and Zone / Zone / Read. Save it as the `CLOUDFLARE_API_TOKEN` GitHub Actions
secret in `gat516/book`. Do not copy the workstation's expiring OAuth token.
No AWS access keys or Kubernetes credentials are stored in GitHub.

`configure.py` previews the trust policy, permissions and SSM document by default.
`--apply` creates or updates those resources only. Run it again after changing
node.py/config.json; CI cannot rewrite its own AWS permissions or SSM document.
AWS trust is restricted to `gat516/book` on `main`; the role can publish only
the six application repositories and invoke only this document on the existing node.

## Recovery

Before changing images, the node records all previous image references and the
public reader version under `/root/qireadr/releases/cd-COMMIT/release.json`.
Images are deployed by digest. A failed rollout or backend verification restores
the prior images and checks both Kubernetes readiness and the previous public
version. Maintenance jobs are suspended during release and restored to their
original suspension state on success or verified recovery.

GitHub separately captures the active Cloudflare version. If frontend deployment
or final verification fails, recovery restores its previous version and the AWS
images. The workflow remains failed after successful rollback. Unknown recovery
is never reported as success. A newer or manually changed workload is not
overwritten by an older release's recovery.

To restore a known release through an authorized operator session:

```bash
AWS_PROFILE=book python3 deploy/hosted/cd/ssm.py rollback \
  --commit FULL_COMMIT_SHA --db-tree APPROVED_DB_TREE
```

An interrupted transaction must be restored before retrying. A completed rollback
can be retried; previous records are retained. If the GitHub runner is lost after
AWS succeeds, inspect the SSM command and Cloudflare version before intervening.
The two platforms are not an atomic deployment transaction.

## Monitoring and checks

GitHub's `Release` run records build, deployment, verification and recovery.
The k8s-platform status API can observe `release.yml`; Grafana independently checks
the public website and reader API. Passing HTTP probes do not prove worker progress.

```bash
python3 -m unittest discover -s deploy/hosted/cd -p 'test_*.py'
```

These tests simulate Kubernetes and Cloudflare. Live deployment is verified by
the workflow; live failure/recovery should not be claimed until exercised.
