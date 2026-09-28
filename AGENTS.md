# Working in this repository

Where to pick up: [`docs/handover.md`](docs/handover.md).

## How work is done here

Every change is tracked as a GitHub issue and lands through a pull request.

1. Pick an open issue. It must state acceptance criteria. If it does not, write them into the issue first.
2. Implement the smallest change that satisfies those criteria.
3. Run the checks, then open a pull request whose body says `Closes #<issue>`.
4. Merge when CI is green. Nothing merges with a red or missing check.

Do not add work the issue does not ask for. Prefer deleting and simplifying over adding. Do not introduce an abstraction before a second caller needs it. Do not add a dependency, a configuration knob, or a migration path "for later".

If the acceptance criteria cannot be met as written, or the change needs a product decision, say so on the issue instead of guessing.

## Conventions

- **Language.** Code comments, commit messages, and technical decision documents are English. User-facing interface documentation may be Turkish.
- **Commits.** Conventional commits: `feat(scope): …`, `fix(scope): …`, `refactor(scope): …`, `test(scope): …`, `docs(scope): …`, `chore(scope): …`, `ci: …`.
- **Branches.** `<type>/issue-<number>-<short-slug>`, for example `feat/issue-80-opencode-adapter`.
- **Lint.** `ruff` with the rule set declared in `pyproject.toml`. It is deliberately `E4`, `E9`, `F`: correctness, not formatting. Widening it is its own change; see #104.
- **Tests.** No test may reach a live model service. Providers are injected through `ProviderRegistry(...)`; use a fake.
- **The contract.** `contracts/openapi.json` is generated, never hand-edited. Regenerate with `python scripts/export_openapi.py`; a test fails when it drifts (#70).
- **Security behaviour is policy, not a flag.** A feature flag may add behaviour; it may not weaken or disable a check. See `src/agent_runtime_platform/features.py`.
- **Boundaries.** Follow `docs/architecture/repo-and-package-boundaries.md`: a language difference is a package boundary, not a repository boundary.

## Providers

Every provider is one directory under `src/agent_runtime_platform/providers/` with the same shape:

- `provider.py` implements the contract in `_base.py`;
- `manifest.toml` declares its capabilities (`id`, `runtime`, `sdk`, `supports_tool_ids`).

Each provider uses its own **official SDK in the stack that SDK targets**. Capability differences are declared in the manifest and refused explicitly with `422`, never silently adapted. Add a manifest field only when something reads it.

## Checks

```sh
uv sync --extra dev            # first time
uv run ruff check .            # lint; CI runs uvx ruff@0.16.9
uv run pytest -q               # python tests
node --test tests/*.test.js    # ui tests
```

`.github/workflows/ci.yml` runs the same three checks on Linux and is the authority.

Local Windows runs differ: `os.fchmod`, POSIX mode bits and `fcntl` are unavailable, so a handful of tests cannot pass there. If you change dependencies, run `uv lock` and commit `uv.lock`; CI installs with `--locked`.
