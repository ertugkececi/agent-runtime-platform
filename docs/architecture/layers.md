# Layer directories

**Decision date:** 2026-09-29

**Status:** The backend package is split into four layers plus two composition
roots. The split is a directory move only: behaviour, module contents and the
public HTTP contract are unchanged (#103).

## The layers

| Layer | Contents | May import |
| --- | --- | --- |
| `api/` | the FastAPI application (`app.py`), request/response schemas (`schemas.py`), the exported contract (`openapi_contract.py`) | `application/`, `domain/`, `infrastructure/` |
| `application/` | use cases and policy that orchestrate the domain and the adapters: `runtime.py`, `rooms.py`, `resource_auth.py` | `domain/`, `infrastructure/` |
| `domain/` | the persisted domain model: entities, identity and clock helpers (`models.py`) | nothing |
| `infrastructure/` | adapters to everything outside the process: `database.py`, `auth.py`, `a2a.py`, `mcp_tools.py`, `queue_metrics.py`, `codex_home.py`, `codex_login.py`, the offline migrations, and `providers/` | `domain/` |

Dependencies point inward:

```
api ──▶ application ──▶ domain
 │            │            ▲
 └────────────┴─▶ infrastructure ─┘
```

- **`domain` depends on no other layer.** It may become the pure-rule home as
  rules move out of the modules that currently mix them with persistence.
- **`infrastructure` depends on `domain`**, never on `application` or `api`.
- **`application` depends on `domain` and `infrastructure`.** Policy that must
  read persisted state (resource authorization) lives here, not in `domain`.
- **`api` is the outermost layer**; it is the only place allowed to translate
  HTTP concerns and to wire the layers at request level.

`features.py` stays at the package root. Every layer may read it; adding a flag
is a change to that module alone. It is the cross-cutting flag registry
(#71) and is deliberately outside the layer graph.

## Composition roots

`main.py` (the ASGI entry point) and `queue_worker.py` (the worker process) sit
at the package root, above the layers. They are the only modules that compose
every layer: the worker builds the application through `api.app.create_app`
and drives it with the queue claim/recover helpers it also exposes to tests.
Keeping them outside the four directories is what keeps the layer graph
acyclic; nothing inside a layer imports them.

`static/index.html`, the minimal console the API serves, stays at the package
root as packaged data; the `api` layer resolves it relative to its own file.

## Where a module goes

A new module belongs to the layer of the thing it adapts or orchestrates, not
of the code that calls it. Concretely:

- talking to a database, a subprocess, an SDK or a remote service is
  `infrastructure/`;
- resolving a request into domain changes is `application/`;
- validating transport shapes and mapping them to use-case calls is `api/`;
- an entity or a rule that holds with no process running is `domain/`.

## References

- Repository and package boundaries: [repo-and-package-boundaries.md](repo-and-package-boundaries.md)
- Provider shape and capability manifests: [../opencode-provider.md](../opencode-provider.md), [../../AGENTS.md](../../AGENTS.md)
