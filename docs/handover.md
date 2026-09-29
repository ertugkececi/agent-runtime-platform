# Handover

Written 2026-09-28, updated 2026-09-29 so this work can continue from another
machine without re-deriving context. The GitHub issues are the plan; this file
only records things that are not yet written down anywhere else.

## Where the work is tracked

| Epic | Scope | State |
| --- | --- | --- |
| **#66** | Backend skeleton and contract foundation | **complete (7/7)** |
| **#67** | OpenCode model provider | **complete (7/7)** |
| **#68** | Frontend (React console) | **complete (8/8)** |
| **#69** | Security and assurance | next — sub-issues #92–#95 |

Follow-ups found while working: **#103** (layered directories) and **#104**
(mypy gate); **#98** (a2a poll deadline) is closed. **#38** and its draft pull
request **#39** are both closed (decision 2026-09-29): the tenant-role work is
re-implemented in **#92**.

## What is on `main` now

- Providers are one directory each under `src/agent_runtime_platform/providers/`,
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

## Next: Epic #69 — Security and assurance

Epic #68 is delivered: the frontend is complete in the separate
`agent-runtime-console` repository and no further frontend work is planned
here. Epic #69 collects the security and assurance work; the sub-issues are
worked in this order:

**#92 → #93 → #94 → #95**

#92 re-applies the tenant-role and published-agent policy from the closed
#38/#39, default-off, and keeps OIDC/mapping plus PostgreSQL/restore
verification as release gates, collected in #93.

## Environment notes

- The machine this work started on blocked `uv.exe` and `ruff.exe` through
  corporate application control, so `uv` never ran there. Python came from the
  MSI installer and dependencies from `pip install -e ".[dev]"`.
- **`uv.lock` has still never been regenerated** — Epic #67 added no Python
  dependency. Nothing in it is stale today. If you add one, run `uv lock` and
  commit the result: CI installs with `uv sync --extra dev --locked`.
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
