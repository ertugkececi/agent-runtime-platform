# Handover

Written 2026-09-28, closed out 2026-09-29 so this work can continue from
another machine without re-deriving context. The GitHub issues were the plan;
this file only records things that are not yet written down anywhere else.

## Where the work is tracked

| Epic | Scope | State |
| --- | --- | --- |
| **#66** | Backend skeleton and contract foundation | **complete (7/7)** |
| **#67** | OpenCode model provider | **complete (7/7)** |
| **#68** | Frontend (React console) | **complete (8/8)** |
| **#69** | Security and assurance | **complete (4/4)** |

Follow-ups found while working are complete and on `main`: **#103** (layered
directories, `docs/architecture/layers.md`) and **#104** (mypy gate,
`docs/type-checking.md`); **#98** (a2a poll deadline) is closed. **#38** and
its draft pull request **#39** are both closed (decision 2026-09-29): the
tenant-role work was re-implemented in **#92** and is on `main` default-off.

## What is on `main` now

- Providers are one directory each under `src/agent_runtime_platform/infrastructure/providers/`,
  with declared capability manifests (#72, #73).
- `features.py` is the single feature-flag registry, default-off (#71).
- `contracts/openapi.json` is the generated HTTP contract, with a drift test (#70).
- `.github/workflows/ci.yml` runs lint, Python tests and UI tests on Linux (#74).
- `docs/architecture/repo-and-package-boundaries.md` records the boundary rule (#76).
- `tests/test_provider_errors.py` guards the provider error family (#75).
- `AGENTS.md` records how work is expected to be done here.
- The OpenCode model provider is on `main`, default-off behind
  `AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE` (#81, #82). It exposes
  `GET /opencode/models`, runs one short-lived TypeScript host process per call
  in a private `0700` home that never reads `~/.config/opencode`, and bounds
  every call with `AGENT_RUNTIME_OPENCODE_TIMEOUT_SECONDS` (default 600 s,
  capped at 3600 s). `docs/opencode-provider.md` is the setup and limits
  reference (#83).
- The React console lives in the separate `agent-runtime-console` repository:
  Vite + React + TypeScript, with its API client generated from
  `contracts/openapi.json` (`openapi-typescript` + `openapi-fetch`), plus the
  design system and accessibility work (#84–#91; console PRs #1–#8).
- Opt-in tenant membership roles are on `main`, default-off (#92):
  `AGENT_RUNTIME_RESOURCE_AUTH_MODE=tenant_roles` with `AGENT_RUNTIME_AUTH_MODE=oidc`
  resolves the principal from exactly one active `tenant_memberships` row,
  applies admin/member plus published-agent policy, rechecks queued work, and
  denies the process-global catalogs. The offline SQLite-only
  `agent-runtime-tenant-role-migrate` command applies the `tenant_roles_v1`
  schema; startup never migrates and PostgreSQL is rejected fail-closed.
  `docs/tenant-roles.md` is the reference.
- `docs/release-gates.md` is the single release-gate checklist (#93): live
  tenant backfill and PostgreSQL verification, OIDC against a real provider,
  restore from backup, and resource-authorization mapping. All four gates are
  open; each closes only on recorded evidence and an approver's statement, and
  the configuration it protects keeps its default-off value until then.
- `agent-runtime-queue-metrics` prints the read-only queue and model-call
  metrics report (#95) over a stated observation window;
  `docs/queue-scaling-decision.md` records what it measures and that it never
  carries content, prompts, credentials or error messages.

## What remains

The plan is delivered: epics **#66–#69** are complete, the follow-ups **#103**
and **#104** have landed, and no open issue remains in this repository or in
`agent-runtime-console`. New work starts as a new issue with acceptance
criteria. Two standing gates can bring work back; both are in this repository:

- **Release gates** ([release-gates.md](release-gates.md)): live tenant
  backfill and PostgreSQL verification, OIDC against a real provider, restore
  from backup, and resource-authorization mapping. Each gate names its
  evidence and approver; until it closes, the configuration it protects keeps
  its default-off value.
- **Queue scaling** ([queue-scaling-decision.md](queue-scaling-decision.md)):
  re-evaluated only when measured use misses an agreed service target or a
  multi-host/availability requirement appears. Collect the measurements with
  `agent-runtime-queue-metrics` before reconsidering the decision.

## Environment notes

- The machine this work started on blocked `uv.exe` and `ruff.exe` through
  corporate application control, so `uv` never ran there. Python came from the
  MSI installer and dependencies from `pip install -e ".[dev]"`.
- `uv.lock` was regenerated when the mypy dev dependency was added (#104). If
  you add a dependency, run `uv lock` and commit the result: CI installs with
  `uv sync --extra dev --locked`.
- **`bun` is needed only for real OpenCode turns and catalog reads.** The
  Python tests run against `tests/fixtures/fake_opencode_host.py` and need no
  bun install; the host package's own suite is the separate `opencode-host` CI
  job.
- CI on Linux is the authority for the test suite. Windows cannot run the
  `fcntl`, `os.fchmod` and file-mode tests, and a timing-sensitive a2a test is
  unreliable there; all of them pass on Linux.
- `gh` is authenticated as `ertugkececi` with the `repo` and `workflow` scopes.
  Git credentials are configured per repository (`.git/config` uses
  `!gh auth git-credential`), so the machine's global git identity is untouched.
