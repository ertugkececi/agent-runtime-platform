# Type checking

**Decision date:** 2026-09-29

**Status:** `uv run mypy` is a required check. Every module under
`src/agent_runtime_platform` type-checks with zero errors; CI runs the same
command in the `typecheck` job.

## The gate

`[tool.mypy]` in `pyproject.toml` is the single definition of the gate:

- `files` is the covered set. CI and developers run bare `uv run mypy`, so the
  set has one definition. mypy reports errors for the modules under a listed
  path; imported modules are read for their types but reported only when their
  own path is listed too. Coverage therefore grows one path at a time.
- `python_version = "3.11"` matches the `requires-python` floor, so the result
  does not depend on the interpreter that happens to be installed.
- `warn_unused_ignores = true` makes a stale exception fail the gate:
  an ignore that is no longer needed is removed, not left behind.
- The gate runs on Linux, the platform CI and production run on. See
  [Platform](#platform) for what a Windows run would add.

## Rules for a covered module

1. **Fix the code, not the checker.** Errors are resolved by narrowing
   (`isinstance`, explicit `None` checks), by annotations the checker was
   missing, or by restructuring a branch so each variable has one type. A
   module is only listed in `files` once it passes without exceptions.
2. **No blanket `# type: ignore`.** An exception must name its error code,
   carry a comment that states why the code is correct, and be one a future
   mypy release can retire without anyone guessing. There is exactly one in
   the tree today (below).
3. **A limitation is recorded here, not hidden.** When the checker cannot see
   something a dependency guarantees, the module gets the smallest explicit
   annotation that expresses the invariant, and this document records why.
4. **Never weaken a check to satisfy the gate.** If a change would silence a
   real error, the error is the finding; the check stays.

## Expanding the covered set

`files` is a list because coverage grows one path at a time. A listed path is
checked and its errors fail the gate; the modules it imports are read only for
their types, so a path can hold the bar on its own without fixing everything it
imports. Tests, scripts and the OpenCode host are not part of the set today;
adding any of them is its own change.

## What the first pass found (2026-09-29)

Starting point on the layered layout (#103): `uv run mypy src` reported **217
errors in 12 files** (the issue measured 206 in 12 files before the layout
split). The gate now reports zero. **No runtime defect was among them**: every
error was an unstated invariant, a missing type, or a checker limitation. The
code paths were correct; where a guard was added it fails closed with a
`RuntimeError` or `ProviderError` instead of an `AttributeError`.

The errors fell into four categories.

| Category | Where | Resolution |
| --- | --- | --- |
| Real defect | — | none found |
| Missing narrowing | `runtime.py`, `rooms.py`, `auth.py`, `queue_worker.py`, `a2a.py` | Code fixes: explicit `None` guards, helper loaders, renamed reloaded rows |
| Missing declaration | `api/app.py`, `runtime.py` | `resource_auth` / `room_runtime` are injected by `create_app`; they are now declared as optional class attributes instead of being attached dynamically |
| Missing annotation | `_manifest.py`, `tenant_migration.py`, `queue_metrics.py` | Typed `_require` (bounded `TypeVar`), annotated counters/sets, annotated generic rows |
| Checker limitation | `openai/provider.py`, `opencode/provider.py`, `queue_metrics.py` | One scoped ignore, optional-kwargs construction, explicit `Any` where SQLAlchemy cannot type a class selected through a variable |

Notable concrete findings:

- **`codex/provider.py` (113 of the 217 errors)** matched SDK thread items by
  reading a `type` string with `getattr`. The items are a sealed union, so the
  module now narrows with `isinstance(item, McpToolCallThreadItem)` /
  `AgentMessageThreadItem` and compares `phase` against the `MessagePhase`
  enum. The conditional kwargs for `thread.turn(...)` became two explicit
  calls, which is also what the provider test asserts.
- **`runtime.py`** assumed `session.get(...)` never returns `None` in a dozen
  places. The invariant is now stated once, in `_require_task` and
  `_require_chat_run`, which fail closed with a `RuntimeError` instead of an
  `AttributeError` if a row ever disappears.
- **`a2a.py`** carried `self._endpoint: str | None` into string concatenation
  after `validate_card()` set it. Both call sites now resolve a local
  endpoint and raise `A2AError("remote_target_unavailable")` if it is missing.
- **`opencode/provider.py`** wrote to `process.stdin` without a `None` check
  and passed `umask=None`, which the POSIX signature types as `int`. The write
  fails closed now and `umask` is only passed where it applies (POSIX).
- **`auth.py`** and `queue_worker.py` passed values mypy typed `str | None`
  into `str` parameters after checks that lived in another statement; the
  checks now narrow the value directly.

### The one exception

`openai/provider.py` passes a plain API-key string to `ChatOpenAI`. LangChain
documents that form and its pydantic validator wraps the string in `SecretStr`
at runtime, but the field annotation does not admit `str`. The ignore names the
error code and the reason, and `warn_unused_ignores` retires it if the
annotation is ever widened:

```python
# The field annotation omits plain strings, but its pydantic
# validator accepts one; LangChain documents a string key.
api_key=api_key,  # type: ignore[arg-type]
```

### Where `Any` is deliberate

`queue_metrics.py` reads three run families through a mapping of model classes
(`Run`/`HumanChatRun`/`RoomRun` and their event tables). SQLAlchemy's stubs
cannot type a class that is selected through a variable, so the loaded rows are
annotated `list[Any]` with a comment. The column names are asserted by
`tests/test_queue_metrics.py`; this is the only place the gate does not check
attribute access, and it is kept to two lines.

## Platform

On Windows the same command reports four errors in `queue_worker.py`:
`os.getuid`, `fcntl.flock`, `fcntl.LOCK_EX` and `fcntl.LOCK_NB`. Those are
POSIX-only APIs the worker's advisory lock needs; the errors are a platform
false positive, not a defect. The gate is defined for Linux, so no
per-platform exception is added. A Windows gate would first have to put those
calls behind an explicit platform check, which is a behaviour change and not
part of this work.

## Running it

```sh
uv run mypy           # the gate, over the set declared in pyproject.toml
uv run mypy <path>    # a single module while working on it
```

## References

- How work lands in this repository: [../AGENTS.md](../AGENTS.md)
- CI definition: [../.github/workflows/ci.yml](../.github/workflows/ci.yml)
- Layer directories, the split the first pass followed:
  [architecture/layers.md](architecture/layers.md)
