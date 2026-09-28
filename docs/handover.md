# Handover

Written 2026-09-28 so this work can continue from another machine without
re-deriving context. The GitHub issues are the plan; this file only records
things that are not yet written down anywhere else.

## Where the work is tracked

| Epic | Scope | State |
| --- | --- | --- |
| **#66** | Backend skeleton and contract foundation | **complete (7/7)** |
| **#67** | OpenCode model provider | next — sub-issues #77–#83 |
| **#68** | Frontend (React console) | not started — #84–#91 |
| **#69** | Security and assurance | not started — #92–#95 |

Follow-ups found while working: **#98** (a2a poll deadline), **#103** (layered
directories), **#104** (mypy gate). **#38** stays open until the draft pull
request **#39** lands.

## What is on `main` now

- Providers are one directory each under `src/agent_runtime_platform/providers/`,
  with declared capability manifests (#72, #73).
- `features.py` is the single feature-flag registry, default-off (#71).
- `contracts/openapi.json` is the generated HTTP contract, with a drift test (#70).
- `.github/workflows/ci.yml` runs lint, Python tests and UI tests on Linux (#74).
- `docs/architecture/repo-and-package-boundaries.md` records the boundary rule (#76).
- `tests/test_provider_errors.py` guards the provider error family (#75).
- `AGENTS.md` records how work is expected to be done here.

## Next: Epic #67 — OpenCode provider

Decisions already taken. Do not re-litigate them; #77 records the reasoning.

1. **Official SDK only.** `@opencode/sdk`, in its native TypeScript stack. No
   hand-written REST wrapper, no Python bridge, no CLI shortcut.
2. **Per-call runner, not a daemon.** The Python adapter starts a short-lived
   TypeScript program per turn: JSON request on stdin, then `OpenCode.create()`,
   `sessions.prompt()`, and a JSON result plus events on stdout, then exit. This
   mirrors how the Codex provider already works (`tempfile` plus `with Codex(...)`).
   A long-lived host is deferred until a measurement asks for it.
3. **`tool_ids` are refused in the first slice** with an explicit `422`, exactly
   as the OpenAI provider does. MCP mapping is a separate change.
4. **Isolation.** OpenCode must not inherit the user's `~/.config/opencode`. Use a
   private directory with `0700`/`0600`, the way `codex_home.py` does for Codex.
5. **Order.** #77 (bridge contract) → #78 (TypeScript package) → #79 (its CI job)
   → #80 (Python adapter) → #81 (`GET /opencode/models`) → #82 (isolation and
   secret minimisation) → #83 (tests and documentation).

What #77 must pin down: the stdin/stdout JSON shape; the event stream carrying
only `{server, tool, status, phase}` and never tool arguments or results; the
error shape; and a bridge protocol version with a compatibility rule.

The flag that will gate all of it already exists: `AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE`
(`provider_opencode` in `features.py`), default off.

## Environment notes

- The machine this work started on blocked `uv.exe` and `ruff.exe` through
  corporate application control, so `uv` never ran there. Python came from the
  MSI installer and dependencies from `pip install -e ".[dev]"`.
- **`uv.lock` was therefore never regenerated.** Nothing in it is stale today
  because no dependency was added. If you add one, run `uv lock` and commit the
  result: CI installs with `uv sync --extra dev --locked`.
- CI on Linux is the authority for the test suite. Windows cannot run the
  `fcntl`, `os.fchmod` and file-mode tests, and a timing-sensitive a2a test is
  unreliable there; all of them pass on Linux.
- `gh` is authenticated as `ertugkececi` with the `repo` and `workflow` scopes.
  Git credentials are configured per repository (`.git/config` uses
  `!gh auth git-credential`), so the machine's global git identity is untouched.
