# Private hosted reader implementation

Accepted target: invite-only Google login, isolated private libraries and BYOK; approved public HTTPS custom providers. EC2/k3s + single-AZ RDS + S3 + persistent Redis. No sharing, SQS, billing, or mandatory gateway. AWS-specific configuration remains in deployment. Spec §0 and chapter gating continue to apply independently of account ownership.

Commit milestones:
- [x] Honorific prompt (29d94d7).
- [x] Account schema and ownership RLS (`92d1898`), portable bootstrap through 0125.
- [x] Google authentication, invitations and session UI (`f4a2edb`).
- [x] API, credentials, embedding and cache isolation (`f669cc5`).
- [x] Scheduling, limits, usage and cancellation (`f669cc5`).
- [x] Migration, durable deletion and restore (`f36de8d`).
- [x] Production containers, k3s manifests, Terraform and CI configuration.
- [x] Local integration, isolation, recovery and container HTTP checks.
- [x] Login-free localhost compatibility (`3181dcb`, `650819b`) and safe upgrade launcher.
- [x] Root/service docs, deployment/recovery runbooks and agent navigation.
- [x] AWS infrastructure, private database bootstrap, image publication, and initial app deployment.
- [ ] Public DNS/TLS, real OAuth consent/callback, and scheduled-job verification.

## Verified locally

- Real restricted-role HTTP requests: two accounts, forged actor headers, cross-owner
  book/config reads, CSRF, private book creation, masked keys, queue controls, deletion,
  and local mode with no login. Real pipeline and Ask AI containers also start with their
  restricted database logins; Ask AI readiness checks the database without model calls.
  Google exchange is covered by shared auth integration
  tests; this is not evidence that an unconfigured real OAuth client works.
- SQL owner isolation and independent source-chapter gates, including missing/forged
  scope and deleted accounts. Bootstrap and incremental migration use disposable DBs.
- Portable snapshot/restore against PostgreSQL 16 and versioned MinIO: hashes, private
  prose, minimal restored permissions, revoked sessions, all-version object deletion,
  newer deletion journal replay, progress migration and credential rotation.
- An isolated copy of the existing local library retained 28 chapters, 155 facts and
  89 glossary entries; all four saved provider credentials still decrypted after the
  account migration. The running local database was left on its existing schema.
- Shared provider suite: 107 passing. Focused pipeline checks: 48 passing, DB-dependent
  cases skipped; the Redis account claim/reaping integration passes separately.
  Ask AI: 19 passing; web: 8 passing and production build. Go checks and Rust image test
  stage pass. Terraform validates; 33 standard Kubernetes resources validate, with the
  k3s-specific HelmChartConfig excluded from generic schema validation.

## Rollout inputs and remaining external checks

The chosen site is **https://qireadr.com**, with domain/DNS at Namecheap and application
hosting on AWS. Google callback: `https://qireadr.com/api/auth/callback`.
Region is **us-east-1**. Initial owner is **charlesj.gatchalian@gmail.com**, superseding
the earlier email. AWS resources are now provisioned. The Free plan required an
`m7i-flex.large` application node, `db.t4g.micro` database, and one-day RDS retention.
The existing account budget is used; Terraform does not create a duplicate budget.
Google credentials are configured, and an initial-owner invitation is saved privately
in `deploy/hosted/generated/owner-invitation.txt`; no invitation email has been sent.
The first hosted database is empty. Local books and services were left in place and
remain usable without Google login.

Deployment details (September 20, 2026):

- Elastic IP: `3.221.223.180`; Namecheap `@` must resolve to this exact address.
- EC2: `i-0d907def09bd3092f`, k3s `v1.35.8+k3s1`.
- RDS: `private-books.cy9a0qa8c4l2.us-east-1.rds.amazonaws.com`; schema through 0125.
- Data bucket: `private-books-data-47b61f1fc61bda74efd975b353`.
- Container images: `964862484671.dkr.ecr.us-east-1.amazonaws.com/private-books/SERVICE:ebd1d43`.
- Node release files: `/root/qireadr/`; management uses SSM, not public SSH.
- Terraform state and variables are private, ignored local files under
  `deploy/hosted/terraform/`. Keep that state when continuing this deployment.
- Local AWS CLI: `/tmp/qireadr-tools/bin/aws`, profile `book`; Terraform:
  `/tmp/qireadr-tools/terraform/terraform`. Reinstall these tools if `/tmp` is cleared.
- Application health, unauthenticated library rejection (including forged identity),
  Google redirect/callback configuration, and secure auth-cookie flags pass against
  the AWS IP. Public DNS/certificate verification is still pending.
- All nine application pods are Ready; Terraform reports no changes after the
  bootstrap corrections. The account-cleanup CronJob has also completed once.

The actual rollout caught Amazon Linux's `awscli-2` package name and Kubernetes'
automatic `ASKAI_PORT=tcp://...` injection. Node bootstrap is now repeatable;
application pods set `enableServiceLinks: false` and use explicit configuration/DNS.

Follow [the hosted runbook](../deploy/hosted/README.md), then verify DNS/TLS, actual
Google signup with the explicit initial-owner invite, a second invited account, RDS
permissions, node S3 credentials, ECR pulls and one scheduled backup/restore in AWS.
CI configuration is committed; its hosted GitHub run is not claimed until it executes.
The stack accepts maintenance outages, shares one trusted node IAM boundary, and does
not provide automatic failover. Preserve encryption keys independently of snapshots.
