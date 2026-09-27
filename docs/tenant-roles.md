# Tenant membership roles (Issue #38)

This is an opt-in SQLite-only authorization mode. The default remains `AGENT_RUNTIME_RESOURCE_AUTH_MODE=off`; the existing `legacy_owner` mode and its schema/guard behavior are retained. Set `AGENT_RUNTIME_AUTH_MODE=oidc` and `AGENT_RUNTIME_RESOURCE_AUTH_MODE=tenant_roles` together only after the offline role migration has been reviewed and backed up. Startup validates the revisions and schema but never applies a migration. PostgreSQL is rejected fail-closed.

## Offline migration

The `agent-runtime-tenant-role-migrate` command accepts an explicit SQLite URL and OIDC issuer/legacy subject/tenant mapping. Run its read-only dry-run first, then pass `--apply` and an explicit `--backup-dir` for the generated pre-migration backup. It requires `tenant_ownership_v1` and its matching mapping digest. It creates `tenant_memberships`, seeds the configured legacy owner as an active admin, and adds `agents.published` with physical default true so existing agents remain published. It records `tenant_roles_v1` and is idempotent only while the schema and migration digests remain valid. Apply only to an offline copy; do not use this command against the live service database.

Membership identity is the verified OIDC `(issuer, subject)` pair. A request requires exactly one active membership across tenants; ambiguous or absent membership is denied. The current role is re-resolved on each request, so deactivation/role changes invalidate or update existing sessions. Database/schema lookup errors fail closed and are surfaced as unavailable, not treated as an unknown user.

## Authorization policy

Admins can list/manage agents in their tenant, including unpublished agents, and create agents. Members see only enabled, published agents in their tenant; hidden or foreign agents are indistinguishable (404), while attempts to mutate a visible agent or create an agent are forbidden (403). Conversations, rooms, runs, children and queue operations remain bound to the creating membership's owner ID plus tenant ID. A worker rechecks that membership and the target agent's current enabled/published state before invocation. Global process catalogs (`/codex/models`, `/mcp/tools`, `/a2a/targets`) are denied in tenant-role mode.

Root creation uses the existing legacy physical default only as a transaction-local starting value, then assigns the authenticated membership owner and tenant with SQL and asserts the binding before commit. SQLite write reservation is acquired before the active-membership recheck. Any failure rolls back the entire create transaction; no intermediate default ownership is committed or observable.

The `published` property is read/written through conditional raw SQL rather than an ORM mapping, so off/legacy operation against pre-role schemas remains compatible.