# Private hosted reader implementation

Accepted target: invite-only Google login, isolated private libraries and BYOK; approved public HTTPS custom providers. EC2/k3s + single-AZ RDS + S3 + persistent Redis. No sharing, SQS, billing, or mandatory gateway. AWS-specific configuration remains in deployment. Spec §0 and chapter gating continue to apply independently of account ownership.

Commit milestones:
- [x] Honorific prompt (29d94d7).
- [ ] Account schema, ownership RLS, portable bootstrap.
- [ ] Google authentication, invitations and session UI.
- [ ] API, credentials, embedding and cache isolation.
- [ ] Scheduling, limits, usage and cancellation.
- [ ] Migration, durable deletion and restore.
- [ ] Production containers, k3s, Terraform and CI.
- [ ] Integration, isolation, recovery and hosted smoke checks.

No public rollout until two-account isolation tests pass. Actual AWS provisioning needs a domain, Google OAuth client, initial owner identity, AWS access and a reviewed cost estimate. Existing local services are not restarted against a partially migrated schema.
