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
- [ ] Actual AWS rollout, real OAuth consent/callback, TLS issuance, node IAM and scheduled-job verification.

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

Region is **us-east-1**. Initial owner is **charlesj.gatchalian@gmail.com**, superseding
the earlier email. No invitation has been sent and no public AWS resources have been
provisioned by this implementation. Domain, Google OAuth client/secret, AWS access and
a current reviewed cost estimate remain necessary. Localhost remains usable without
Google login; its existing services were not restarted for this work.

Follow [the hosted runbook](../deploy/hosted/README.md), then verify DNS/TLS, actual
Google signup with the explicit initial-owner invite, a second invited account, RDS
permissions, node S3 credentials, ECR pulls and one scheduled backup/restore in AWS.
CI configuration is committed; its hosted GitHub run is not claimed until it executes.
The stack accepts maintenance outages, shares one trusted node IAM boundary, and does
not provide automatic failover. Preserve encryption keys independently of snapshots.
