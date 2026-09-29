# Release gate checklist (Issue #93)

This is the single checklist scanned before a production deployment. It collects
the gates that were previously only mentioned where they happened to become
relevant: the README roadmap, [tenant-migration.md](tenant-migration.md),
[tenant-roles.md](tenant-roles.md) and the
[security design](inbound-a2a-multiuser-security.md).

| # | Gate | Status |
| --- | --- | --- |
| 1 | Tenant live backfill and PostgreSQL verification | open |
| 2 | OIDC against a real provider | open |
| 3 | Restore from backup | open |
| 4 | Resource authorization mapping | open |

Each gate answers three questions: **what** is verified, **how** it is verified,
and **who approves** closing it. A gate is closed only when its evidence is
recorded and the approver states the closure; until then the configuration it
protects keeps its default-off value (`AGENT_RUNTIME_AUTH_MODE=off`,
`AGENT_RUNTIME_RESOURCE_AUTH_MODE=off`). Evidence records identifiers, counts,
checksums and timestamps — never secrets, tokens or authorization codes.

## 1. Tenant live backfill and PostgreSQL verification

**What.** Applying `tenant_ownership_v1` (and, when tenant roles are enabled,
`tenant_roles_v1`) to the live database: every existing root row is backfilled to
the operator-supplied legacy owner and tenant, with physical `NOT NULL`
owner/tenant columns, the composite FK and pair-check triggers intact, and every
`queue_jobs.run_id` resolving to exactly one owned parent. It also covers the
PostgreSQL path of the migration tool.

**Current state.** No live database has been backfilled. The tools are
offline-only — startup never migrates — and PostgreSQL dry-run and apply are
hard-disabled in the code until service-backed read-only, migration and restore
integration tests exist. The production issuer/subject mapping has never been
supplied, which is why [tenant-migration.md](tenant-migration.md) says not to
apply to live data.

**How it is verified.**

1. On a copy of the live database, run the read-only dry-run
   (`agent-runtime-tenant-migrate`). Record the report: `owner_mapping_sha256`,
   `snapshot` row counts and checksum, `legacy_rows_to_backfill`, and
   `mutated: false`.
2. Apply on a disposable copy with `--apply --backup-dir`. Verify the generated
   backup (mode `0600`, `PRAGMA integrity_check`, checksum equal to the dry-run
   snapshot), the recorded revision, the owned-root invariants, and the queue-job
   resolution. The same invariants are the ones `tests/test_tenant_migration.py`
   asserts for SQLite.
3. Write with the previous release binary against the migrated copy, and read
   with the new binary in modes `off` — the old binary must keep working on the
   new schema.
4. Only then the live apply: maintenance window, API and worker writers stopped,
   the pre-migration backup retained, and a post-apply check of row counts and
   child/queue ownership joins.
5. PostgreSQL: add and pass service-backed read-only, migration and restore
   integration tests, open the two hard-disabled code paths, and run the same
   drill against PostgreSQL. The gate closes only when the code path is open and
   the drill has passed, not merely when a test exists.

**Who approves.** The repository owner, acting as platform operator, after
reviewing the recorded dry-run/apply reports, backup checksum and rollback plan.

## 2. OIDC against a real provider

**What.** The Authorization Code + PKCE S256 login end to end against the real
issuer — not the loopback test issuer used in tests — before any network access
beyond the private boundary is opened.

**Current state.** OIDC ships default-off and every automated check so far has
used a test issuer, as the tests must not reach live services. The README states
that general network access must not be opened until the real provider has been
verified.

**How it is verified.** Register the application as a public client
(`token_endpoint_auth_method=none`, PKCE required) with the exact HTTPS callback
URL, set the `AGENT_RUNTIME_OIDC_*` values from the environment only, and run the
API at that callback origin. Complete a login with the operator identity and
record: discovery and callback behaviour, `/auth/session`, the cookie flags
(`__Host-`, Secure, HttpOnly, SameSite=Lax), CSRF and exact-`Origin` rejection
for a state-changing request, `/docs`, `/redoc` and `/openapi.json` returning
404, logout and re-login, and a second subject being refused. Re-verify after
any issuer, client or redirect URI change.

**Who approves.** The repository owner, acting as platform operator, after
recording the issuer, client ID and redirect URI with the observed results and
the date.

## 3. Restore from backup

**What.** A consistent backup restores into a working service, and the rollback
path — the pre-migration database plus the matching old binary — is known to
work. This is the drill the [security design](inbound-a2a-multiuser-security.md)
requires before a migration and the restore verification the README lists as
open.

**Current state.** No restore has been exercised against a live-shaped database;
the backups the migration tools produce have only been checked inside those
tools.

**How it is verified.**

1. Take a fresh backup with the documented procedure (the SQLite online backup
   the migration tool produces at mode `0600`, or `pg_dump` for PostgreSQL).
2. Restore it to a separate path, never over the live database. Check the
   restore with `PRAGMA integrity_check` (SQLite) and compare row counts and
   checksum with the source.
3. Start the matching binary against the restored copy and exercise the read
   paths: sign-in state, agent list, conversation history and one run trace, plus
   worker startup.
4. Record the date, source, restore target, checksums and command output, and
   retain the backup in operator-controlled storage.
5. Repeat the drill for the post-migration backup, so both rollback and forward
   recovery are demonstrated.

**Who approves.** The repository owner, acting as platform operator, after the
restore and the rollback run have been demonstrated on a copy.

## 4. Resource authorization mapping

**What.** The owner mapping recorded in the live database matches the configured
OIDC identity exactly, and the authorization policy behaves as documented in the
mode that will be enabled: `legacy_owner` or, after the role migration,
`tenant_roles`.

**Current state.** Mapping and route-matrix behaviour are covered by fixtures
(`tests/test_resource_auth.py`, `tests/test_auth.py`,
`tests/test_tenant_roles_migration.py`) but have never been checked against a
staged deployment with the production issuer and subject.

**How it is verified.**

1. Confirm the exact `iss` and `sub` from the gate 2 evidence, and that
   `--tenant-id` equals `AGENT_RUNTIME_OIDC_LEGACY_TENANT`. A mismatch with the
   environment is refused by the migration tools.
2. Confirm `tenant_migration_versions.mapping_sha256` equals the SHA-256 of
   `issuer \0 subject \0 tenant_id` for the applied revision.
3. Start the staged deployment with `AGENT_RUNTIME_AUTH_MODE=oidc` and
   `AGENT_RUNTIME_RESOURCE_AUTH_MODE=legacy_owner` (or `tenant_roles`), and check
   the route matrix: anonymous business request 401; authenticated without the
   required scope or role 403; foreign or absent resource 404; identity claims in
   the body 422; global process catalogs denied under `tenant_roles`; and a
   worker recheck of queued work after a membership or agent-state change.
4. Run the same checks against the staged deployment, using the repository
   suites above as the reference behaviour, and record requests, responses and
   the date.

**Who approves.** The repository owner, acting as platform operator, after the
staged matrix run matches the documented 401/403/404/422 behaviour.

## Pre-deployment scan

Before each production deployment:

1. Scan the table above. A gate is either closed with evidence, or the
   configuration it protects is still at its default-off value.
2. Record the scan date, the deployment, and the per-gate status where the
   deployment is tracked.
3. If a deployment would enable a configuration whose gate is open, it stops
   until the approver closes that gate. Closing is recorded by the approver and
   the status in this document is updated.

This checklist does not replace the staged enablement and review items in the
[security design](inbound-a2a-multiuser-security.md); threat-model/security
review, dependency scan, incoming A2A and the public listener remain separate
release decisions.
