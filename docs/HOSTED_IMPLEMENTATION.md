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
- [x] Public DNS/TLS, origin certificate, and initial owner account claimed through Google.
- [ ] Second invited login and scheduled backup/restore verification in AWS.
- [x] Cloudflare frontend build, same-origin API proxy and privacy checks prepared.
- [x] Cloudflare frontend published as `qireadr-web` with a same-origin HTTPS route.
- [x] Strict origin TLS and proxied-domain verification for the Cloudflare cutover.
- [x] Local library transferred into the owner's existing hosted account.
- [x] Onboarding, demo, library, chapter reader and wiki hovercard UI published.

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

The chosen site is **https://qireadr.com**, with domain registration at Namecheap,
DNS and frontend moving to Cloudflare, and backend hosting on AWS.
Google callback: `https://qireadr.com/api/auth/callback`.
Region is **us-east-1**. Initial owner is **charlesj.gatchalian@gmail.com**, superseding
the earlier email. AWS resources are now provisioned. The Free plan required an
`m7i-flex.large` application node, `db.t4g.micro` database, and one-day RDS retention.
The existing account budget is used; Terraform does not create a duplicate budget.
Google credentials are configured, and an initial-owner invitation is saved privately
in `deploy/hosted/generated/owner-invitation.txt`; no invitation email has been sent.
The owner has claimed the hosted account. Its initially empty library now contains the
September 20 local-library snapshot described below. Local books and services remain
in place and usable without Google login.

Deployment details (September 20, 2026):

- Elastic IP: `3.221.223.180`; the Cloudflare apex record retains this AWS origin.
- EC2: `i-0d907def09bd3092f`, k3s `v1.35.8+k3s1`.
- RDS: `private-books.cy9a0qa8c4l2.us-east-1.rds.amazonaws.com`; schema through 0125.
- Data bucket: `private-books-data-47b61f1fc61bda74efd975b353`.
- Base application images: `964862484671.dkr.ecr.us-east-1.amazonaws.com/private-books/SERVICE:ebd1d43`.
  Reader API uses `reader-api:d9f74ce` for wiki navigation source keys. Backup/cleanup
  use `python:maintenance-translations-20260920` to include pipeline-translated objects.
- Node release files: `/root/qireadr/`; management uses SSM, not public SSH.
- Terraform state and variables are private, ignored local files under
  `deploy/hosted/terraform/`. Keep that state when continuing this deployment.
- Local AWS CLI: `/tmp/qireadr-tools/bin/aws`, profile `book`; Terraform:
  `/tmp/qireadr-tools/terraform/terraform`. Reinstall these tools if `/tmp` is cleared.
- Application health, unauthenticated library rejection (including forged identity),
  Google redirect/callback configuration, and secure auth-cookie flags pass against
  the AWS IP and the public Cloudflare domain. The origin certificate also validates
  with `curl --resolve qireadr.com:443:3.221.223.180 https://qireadr.com/`.
- All nine application pods are Ready; Terraform reports no changes after the
  bootstrap corrections. The account-cleanup CronJob has also completed once.

The actual rollout caught Amazon Linux's `awscli-2` package name and Kubernetes'
automatic `ASKAI_PORT=tcp://...` injection. Node bootstrap is now repeatable;
application pods set `enableServiceLinks: false` and use explicit configuration/DNS.

Cloudflare frontend (September 20, 2026):

- Cloudflare zone is active with `garrett.ns.cloudflare.com` and
  `mia.ns.cloudflare.com`. The apex is proxied with AWS `3.221.223.180` as origin.
  Earlier Namecheap forwarding/stale DNS failures are resolved; HTTPS assets and API
  responses traverse Cloudflare successfully.
- `qireadr-web` version `68b41bf1-9d2e-47da-9151-42d944733167` is published on
  `https://qireadr.com/*`. Its static assets are built from `services/web`;
  `/api/*` still reaches AWS. Six proxy checks, eight existing web test files,
  twelve onboarding/reader component tests, the production build, and asset-hash
  checks pass. Local reader API wiki keys and disposable-DB wiki chapter gates pass.
- The origin TLS certificate validates. The owner configured Full (strict) and
  Always Use HTTPS off; the latter allows HTTP ACME challenges to reach Traefik.
- The owner does not use domain email. Imported forwarding records were retained;
  no email migration was performed.
- Wrangler uses a narrowly scoped OAuth login stored under
  `/tmp/qireadr-cloudflare-auth`; no Cloudflare token is in source or Worker bindings.
  See [the Cloudflare runbook](../deploy/cloudflare/README.md) for future releases,
  verification and DNS-only rollback.

Library transfer (September 20, 2026):

- Copied one novel, 38 chapters (33 with current translations), 223 facts, 99 wiki
  subjects, 90 glossary entries, saved progress through chapter 28, and four saved
  provider keys into `charlesj.gatchalian@gmail.com`'s existing Google account.
- Verified 24 table counts and all 77 object hashes. Keys were decrypted with the
  source key and re-encrypted with the hosted runtime key and account binding;
  Google identity and sessions were preserved. No keys or private prose are committed.
- Source writers were paused only for export and resumed. The hosted account queue
  is paused, and copied active scraping claims are cancelled, so no paid processing
  starts as a side effect. Resume through the account queue controls when ready.
- Rehearsed the import on disposable PostgreSQL/MinIO, including a different owner
  and key, duplicate rejection, ownership RLS and chapter gates. The historical
  `0079_chapter_knowledge.sql` source-only ledger label was explicitly allowed only
  after schema comparison; neither migration ledger was rewritten.
- Encrypted RDS snapshot `private-books-before-library-20260920` is available in AWS.
  The source archive and reports are private files under `/tmp/qireadr-library-transfer/`.
  Preserve provider encryption keys independently of snapshots.
- Backup, restore and durable deletion now cover both `novels/<id>/` and
  `translated/<id>/`. Real versioned-object recovery and deletion replay passed.

UI release `d9f74ce` adds licensed Animate UI motion primitives, onboarding, a public
demo, a paper chapter view and a story companion. Hovercards close on successful save,
Escape/outside click, and link to an existing wiki subject by its saved source key.
Wiki and Ask AI requests narrow to the open chapter on rereads (§0.3). DOM tests cover
the interactions; a real-browser visual pass was unavailable in this operator session.

Follow [the hosted runbook](../deploy/hosted/README.md), then verify a second invited
account and one scheduled backup/restore in AWS.
CI configuration is committed; its hosted GitHub run is not claimed until it executes.
The stack accepts maintenance outages, shares one trusted node IAM boundary, and does
not provide automatic failover. Preserve encryption keys independently of snapshots.
