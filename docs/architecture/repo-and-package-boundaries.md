# Repo and package boundaries

**Decision date:** 2026-09-28

**Status:** One rule decides repository boundaries. A stack or language difference alone never justifies a new repository; it justifies a package boundary inside an existing one.

## The rule

> **A separate repository is warranted when a component has an independent deployable artifact, an independent consumer, and an independent release cadence.**
>
> **A language or toolchain difference is a package boundary, not a repository boundary.**

## Application

| Component | Boundary | Reason |
| --- | --- | --- |
| Backend (API, runtime, worker, provider modules) | Repository | One deployable unit, one consumer group (API clients), one database |
| Frontend (React console) | Repository | Deployed independently, different consumer (browser), independent cadence |
| `codex` provider | Package (`infrastructure/providers/codex/`) | The official SDK is Python; no separate runtime is required |
| `opencode` provider | Package (`infrastructure/providers/opencode/` plus `infrastructure/providers/opencode/host/`) | The official SDK is TypeScript; the host runs in its native stack |
| `contracts/` | Directory in the backend repository | One consumer today; a separate repository needs two or more consumers and independent versioning |

## Why not one repository per host

Splitting every provider host into its own repository (`agent-runtime-codex-host`, `agent-runtime-opencode-host`, `agent-runtime-claude-host`, and so on) would be internally consistent, but each of those repositories would have no independent consumer, no independent release cadence, and no independent deployment artifact. For a single-operator project this is pure overhead: more CI pipelines, more lockfiles, more cross-repository synchronization, and more places for a version skew to hide.

The provider abstraction therefore lives **inside** the backend repository and is expressed through a uniform module shape rather than through repository count.

## Uniform provider shape

Every provider is one directory under `infrastructure/providers/` with the same shape:

```
providers/
  _base.py       # the minimum shared contract
  _registry.py   # registration and policy enforcement
  _manifest.py   # capability manifest types
  <name>/
    provider.py    # adapter implementing the shared contract
    manifest.toml  # capabilities declared, not implied
    host/          # optional: the provider's own runtime package
```

`codex` and `openai` need no `host/` directory because their official SDKs are Python. `opencode` needs one because its official SDK is TypeScript. The shape is identical; only the presence of the optional package differs.

Each provider uses its own official SDK in the stack that SDK targets. The manifest declares real differences instead of hiding them: a capability the provider does not support (for example `tool_ids`) is refused with an explicit `422`, not silently adapted.

## The mixed-language package

`infrastructure/providers/opencode/host/` is a self-contained package inside the Python repository:

- its own `package.json` and lockfile;
- its own build and test commands;
- its own CI job, separate from the Python job.

One repository does not mean one toolchain. A package boundary keeps the TypeScript dependency tree, lockfile, and build isolated from the Python dependency tree, while a repository boundary would only add synchronization cost.

## What would change this decision

Revisit these boundaries when a component acquires an independent consumer or an independent release cadence. Concretely:

- a provider host is published for use outside this platform;
- the frontend is consumed by more than the browser application, for example as a published component library;
- `contracts/` is consumed by two or more independent repositories that need to version it separately.

Until then, the boundaries above stand.

## References

- Backend layer directory rule: [layers.md](layers.md)
- Agent registration, providers, and runtime behavior: [README](../README.md)
- Queue scaling decision, which uses the same evidence-gated approach: [queue-scaling-decision.md](queue-scaling-decision.md)
