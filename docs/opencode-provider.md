# OpenCode model provider

**Status:** the one model provider. Every agent runs through OpenCode; there is
no per-vendor provider module. An OpenCode agent is used exactly like any other
agent — human chat, the async queue, group rooms, and the single-level handoff
all go through the same runtime contract. This document is the setup and limits
reference. The wire contract between the Python adapter and the TypeScript host
is [`opencode-bridge-contract.md`](opencode-bridge-contract.md).

The provider speaks the official `@opencode/sdk` in its native TypeScript
stack. There is no hand-written REST wrapper, no Python bridge and no CLI
shortcut.

## How a call runs

One model turn, a catalog read, or a provider sign-in runs one TypeScript
process (`src/agent_runtime_platform/infrastructure/providers/opencode/host/`).
The Python adapter (`providers/opencode/provider.py`, and
`providers/opencode/connections.py` for sign-ins) spawns it with `bun run
start`, writes one JSON request on stdin, reads NDJSON records on stdout, and
treats exit as the end of the call. There is no daemon and no other shared
state between calls.

- The model is named `provider/model`, with an optional `#variant`
  (`opencode/big-model#high`). `model_reasoning_effort` is sent as that variant
  when the model name does not already carry one.
- A turn returns one final reply (or one handoff); nothing streams into the
  application.
- **Tool grants** travel as administrator-approved MCP servers: only the
  granted tools of each server, with credential *names*, never values. The host
  registers those servers, allows exactly those tool actions, and denies
  everything else.
- The model catalog is served as `GET /opencode/models`, in the shared catalog
  shape. It is read from the SDK's bundled snapshot, never from the network; a
  provider without credentials simply contributes no models, so an empty
  catalog is a valid answer. Each catalog request starts one host process; an
  unavailable host answers `503`.
- Tool activity surfaces only as `{server, tool, status, phase}` events.
  Arguments, results, prompts and credentials are never written to the trace,
  to stderr, or to the API.
- Failures (a version mismatch, a host crash, bad output, a timeout) collapse
  into one `ProviderError`, so a run fails explicitly instead of hanging.

## Setup

1. Install **bun 1.4.2 or newer** (the version CI pins) where the server
   process can find it. The host runs TypeScript directly under `bun run
   start`; no separate Node.js install and no build step are required.
2. Install the host dependencies once per deployment, as the user that runs
   the service:

   ```sh
   cd src/agent_runtime_platform/infrastructure/providers/opencode/host
   bun install --frozen-lockfile
   ```

   `node_modules/` and `dist/` are not committed, so this step is required on
   every fresh checkout. `dist/` is not used at runtime.
3. Give the host credentials, either way:
   - **Environment.** Put the provider's standard API key variable in the
     server process environment — the same variables an OpenCode installation
     reads. The bridge passes the server environment through to the host
     unchanged, except that `HOME` and the XDG roots are replaced (see
     [Isolation](#isolation)).
   - **Sign-in.** Run a provider sign-in through the API (see
     [Provider connections](#provider-connections)); the credential is stored
     in the persistent data root and used by every later call.
4. Restart, then create an agent with `"model_provider": "opencode"` and a
   `model_name` from `GET /opencode/models`.

Credentials are never carried in an agent record, a request body, an API
response, or an event record.

## Provider connections

A sign-in is one long-lived host process that starts an OAuth attempt, reports
what the human must do, waits until the provider credential is stored, and
exits. The credential lands in the persistent data root, so it survives the
call and every later turn.

| Route | Purpose |
| --- | --- |
| `GET /opencode/integrations` | The integrations and their sign-in methods, with a `connected` flag. |
| `POST /opencode/connections` | Start one attempt: `{"integration": "openai", "method": "<method id>", "label": "..."}`. `method` defaults to the headless method when omitted. Answers `202` with the attempt. |
| `GET /opencode/connections/{attempt_id}` | The attempt's `status` (`waiting`, `complete`, `failed`), its `url`, `instructions`, and `mode`. |
| `POST /opencode/connections/{attempt_id}/code` | For a `code`-mode attempt: the code the provider showed the human. |
| `DELETE /opencode/connections/{attempt_id}` | Stop the attempt and release its process. |

The default method for `openai` is **ChatGPT Pro/Plus (headless)**: the attempt
answers with a verification URL and a code the human enters there; the host
polls until the account authorizes it. The browser method exists but needs the
human's browser to reach a callback port on the host machine, so the headless
method is preferred. The same budget as a model call
(`AGENT_RUNTIME_OPENCODE_TIMEOUT_SECONDS`) bounds how long an attempt may wait;
an attempt that outlives it fails and its process is killed.

The attempt is owned by the API worker process that started it, because it
holds the subprocess and waits for a human. A deployment with several API
workers must route the follow-up calls of one attempt back to the same worker.

## Isolation

Every call runs in a private home of its own. The adapter creates it with mode
`0700`, points `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and
`XDG_STATE_HOME` inside it, runs the child with umask `077`, and removes the
directory when the call ends. `XDG_DATA_HOME` is the one exception: it points
at the persistent, app-owned data root (`AGENT_RUNTIME_OPENCODE_HOME`, default
`~/.agent-runtime-platform/opencode-home/data`, mode `0700`), which is where
OpenCode stores its credential database and sessions. A host started outside
the adapter fills in unset roots with its own private directory, so the
invoking user's OpenCode configuration is never read or modified. See the
bridge contract for the full property.

## Limits — what is not supported

- **Only administrator-approved read-only tools.** Tool grants are refused at
  agent create/update unless every id is in an administrator-defined server's
  `read_only_tools` list; the host allows exactly those actions and denies
  every other action, including shell, file edits, and web access.
- **No long-lived host for turns.** One process per call, so the host start
  cost is paid on every turn and every catalog read. A persistent host is
  deferred until a measurement asks for it.
- **No streaming.** A turn delivers one final reply (or one handoff request),
  never incremental tokens.
- **No session reuse or memory.** Every turn starts a fresh OpenCode session
  and re-sends the whole history the runtime passes in. The data root keeps
  credentials and sessions, but a turn does not resume one.
- **No provider-specific HTTP surface beyond the routes above.** Agent records
  behave as for any provider.
- **Remote handoff only through the existing runtime rules.** A handoff is
  offered only when the runtime allows one, carries exactly one capability and
  one task, and is matched exactly as any other handoff is.

## Deployment note (bun and systemd)

The API process and the queue worker both spawn the host, so **both** service
definitions need `bun` on their `PATH`.

systemd user services start with a minimal `PATH` that usually does not
include `~/.bun/bin`, where the bun installer places the binary. Every
OpenCode call then fails with "The OpenCode bridge host requires 'bun' on
PATH." Either install bun in a directory the service already searches
(`/usr/local/bin`), or extend the unit, for example:

```ini
[Service]
Environment=PATH=/home/opc/.bun/bin:/home/opc/apps/agent-runtime-platform/.venv/bin:/usr/local/bin:/usr/bin:/bin
```

The host package must be installed on the deployment host as the service user
(`bun install --frozen-lockfile` in
`src/agent_runtime_platform/infrastructure/providers/opencode/host`, as in step 2 above).
`.env` in the service working directory (`/home/opc/apps/agent-runtime-platform`)
is loaded by the app, so the timeout, the data root and the provider
credentials can live there; systemd `Environment=`/`EnvironmentFile=` works as
well. On timeout the adapter kills the host — its whole process group on POSIX
— so no orphaned bun process is left behind.

## Testing

Automatic tests never contact a live model service.

- `uv run pytest` drives the adapter against
  `tests/fixtures/fake_opencode_host.py`, a Python process with the same
  stdin/stdout shape; it needs no bun install. The bridge contract scenarios
  are pinned there: version mismatch, host crash, timeout (including process
  cleanup), corrupted output, reply, handoff, event trace, catalog,
  integrations, and both connection modes, alongside the isolation cases.
- The host package has its own suite under
  `providers/opencode/host/test/`; `bun test` there needs no network either,
  apart from installing dependencies. CI runs it as the separate
  `opencode-host` job.

## References

- [`opencode-bridge-contract.md`](opencode-bridge-contract.md) — the
  stdin/stdout contract, record shapes, version window and isolation property.
- [`../src/agent_runtime_platform/infrastructure/providers/opencode/host/README.md`](../src/agent_runtime_platform/infrastructure/providers/opencode/host/README.md)
  — the host package and its own checks.
- `.env.example` — the OpenCode environment variables.
