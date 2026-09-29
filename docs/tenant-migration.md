# Offline tenant ownership migration (Issue #34)

This is an **offline, operator-run migration**, not an application startup migration. API startup `create_all` and compatibility ALTER statements do not run this migration. Do not set client-supplied tenant or owner identity; neither value is accepted from request bodies. The command requires exact operator-supplied OIDC issuer, subject, and tenant ID. The tenant ID must match `AGENT_RUNTIME_OIDC_LEGACY_TENANT` (default `legacy`). The tool cannot independently verify that issuer/subject are the intended operator claims.

## Data shape and compatibility

Revision `tenant_ownership_v1` creates `tenants`, `tenant_owners`, and `tenant_migration_versions`. Ownership roots are `agents`, `conversations`, and `rooms`. A conversation roots human-chat sessions and their messages/runs/events; an agent roots capabilities; rooms root participants/runs/turns/events. Other run/task/message/event children inherit scope through parent FKs. `conversation_members` identifies agents, not humans.

`queue_jobs.run_id` is polymorphic across `runs`, `human_chat_runs`, and `room_runs` and has no physical FK. Preflight rejects a queue row unless exactly one run-to-root path resolves; post-backfill validation verifies that each job resolves to exactly one owned parent. Resource authorization must retain this typed resolution or replace it with a persisted run-kind/FK design.

There is intentionally one legacy owner during this compatibility window. SQLite roots are rebuilt with physical NOT NULL ownership fields, configured legacy defaults, owner/tenant FKs, composite `(owner_id, tenant_id)` FK, and pair-check triggers. Existing root rows are backfilled. Defaults preserve current single-owner ORM inserts; triggers reject partial or inconsistent claims. The API accepts no ownership fields from callers. This does not enable multi-tenant access or OIDC.

PostgreSQL dry-run and apply are both hard-disabled in the current tool until service-backed read-only, migration, and restore integration tests pass. No PostgreSQL service is available in this work slice.

## Commands

Run during a maintenance window with API and worker writers stopped. Use a restored copy first.

```sh
uv run python -m agent_runtime_platform.infrastructure.tenant_migration \
  --database-url sqlite:////srv/agent-runtime/agent_runtime.db \
  --issuer 'https://issuer.example/' --subject 'stable-sub' --tenant-id 'legacy'
```

After reviewing the report and confirming the exact claims, exercise apply on a disposable copy:

```sh
uv run python -m agent_runtime_platform.infrastructure.tenant_migration \
  --database-url sqlite:////tmp/agent_runtime-copy.db \
  --issuer 'https://issuer.example/' --subject 'stable-sub' --tenant-id 'legacy' --apply \
  --backup-dir /tmp/agent-runtime-backups
```

The migration creates a consistent SQLite online backup exclusively with mode 0600, runs integrity_check, and compares its snapshot checksum. Before mutation it rechecks the snapshot under a write lock; any change aborts. Applying is transactional and versioned. Same mapping is idempotent; different mapping is rejected. SQLite dry-run opens the existing DB read-only and uses a single consistent read snapshot.

The production issuer/subject mapping is not supplied, so do not apply this to live data. Before any future live apply: confirm exact `iss/sub`, ensure `--tenant-id` matches configured `AGENT_RUNTIME_OIDC_LEGACY_TENANT`, stop writers, restore-test backup, review dry-run, apply to a staging copy, verify child/queue ownership joins, test old-binary writes, and retain rollback to the pre-migration DB plus matching old binary. OIDC enablement, resource authorization, PostgreSQL apply, inbound A2A, and public listener remain separate release gates. The collected checklist is [release-gates.md](release-gates.md).
