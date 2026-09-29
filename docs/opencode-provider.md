# OpenCode model provider

**Status:** optional provider, default off. An OpenCode agent is used exactly
like any other agent — human chat, the async queue, group rooms, and the
single-level handoff all go through the same runtime contract. This document is
the setup and limits reference. The wire contract between the Python adapter
and the TypeScript host is [`opencode-bridge-contract.md`](opencode-bridge-contract.md).

The provider speaks the official `@opencode/sdk` in its native TypeScript
stack. There is no hand-written REST wrapper, no Python bridge and no CLI
shortcut.

## How a call runs

One model turn or one catalog read runs one short-lived TypeScript process
(`src/agent_runtime_platform/infrastructure/providers/opencode/host/`). The Python adapter
(`providers/opencode/provider.py`) spawns it with `bun run start`, writes one
JSON request on stdin, reads NDJSON records on stdout, and treats exit as the
end of the call. There is no daemon and no state shared between calls.

- The model is named `provider/model`, with an optional `#variant`
  (`opencode/big-model#high`). `model_reasoning_effort` is sent as that variant
  when the model name does not already carry one.
- A turn returns one final reply (or one handoff); nothing streams into the
  application.
- The model catalog is served as `GET /opencode/models`, in the same shape as
  `GET /codex/models`. It is read from the SDK's bundled snapshot, never from
  the network; a model provider without credentials simply contributes no
  models, so an empty catalog is a valid answer. Each catalog request starts
  one host process; an unavailable host answers `503`.
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
3. Put the model provider's credentials in the server process environment —
   the same variables an OpenCode installation reads (for example a
   provider's standard API key variable). The bridge passes the server
   environment through to the host unchanged, except that `HOME` and the XDG
   roots are replaced by a private directory. The user's
   `~/.config/opencode`, its credential store and its sessions are never
   inherited.
4. Turn the provider on and restart:

   ```dotenv
   AGENT_RUNTIME_FEATURE_PROVIDER_OPENCODE=on
   # Optional. Wall-clock budget per call; default 600, capped at 3600.
   # AGENT_RUNTIME_OPENCODE_TIMEOUT_SECONDS=600
   ```

   Both variables are documented in `.env.example`.
5. Create an agent with `"model_provider": "opencode"` and a `model_name`
   from `GET /opencode/models`. Without the flag the provider is not
   configured: `GET /opencode/models` answers `404` and agent create/update
   answers an explicit `422`.

Credentials are never carried in an agent record, a request body, an API
response, or an event record.

## Isolation

Every call runs in a private home of its own. The adapter creates it with mode
`0700`, points `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and
`XDG_STATE_HOME` inside it, runs the child with umask `077` (files `0600`,
directories `0700`), and removes the directory when the call ends. A host
started outside the adapter fills in unset roots with its own private
directory, so the invoking user's OpenCode configuration is never read or
modified. See the bridge contract for the full property.

## Limits — what is not supported

- **Tool grants.** `tool_ids` are refused with an explicit `422` at agent
  create/update, and the host refuses a non-empty grant before any model work
  starts. The model is given no tools at all: the host installs an all-deny
  permission policy, and it re-checks the effective policy before every turn.
  MCP tool mapping is not implemented for this provider.
- **No long-lived host.** One process per call, so the host start cost is paid
  on every turn and every catalog read. A persistent host is deferred until a
  measurement asks for it.
- **No streaming.** A turn delivers one final reply (or one handoff request),
  never incremental tokens.
- **No session reuse or memory.** Every turn starts a fresh OpenCode session
  and re-sends the whole history the runtime passes in. Nothing survives a
  call.
- **Credentials only through the environment.** There is no login flow inside
  the application; the host cannot see the operator's OpenCode credential
  store.
- **Remote handoff only through the existing runtime rules.** A handoff is
  offered only when the runtime allows one, carries exactly one capability and
  one task, and is matched exactly as a Codex handoff is.
- **No provider-specific HTTP surface.** The only endpoint is
  `GET /opencode/models`; agent records behave exactly as for the other
  providers.

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
is loaded by the app, so the flag, the timeout and the provider credentials
can live there; systemd `Environment=`/`EnvironmentFile=` works as well. On
timeout the adapter kills the host — its whole process group on POSIX — so no
orphaned bun process is left behind.

## Testing

Automatic tests never contact a live model service.

- `uv run pytest` drives the adapter against
  `tests/fixtures/fake_opencode_host.py`, a Python process with the same
  stdin/stdout shape; it needs no bun install. The four bridge contract
  scenarios are pinned there: version mismatch, host crash, timeout (including
  process cleanup), and corrupted output, alongside the reply, handoff, event
  trace, catalog and isolation cases.
- The host package has its own suite under
  `providers/opencode/host/test/`; `bun test` there needs no network either,
  apart from installing dependencies. CI runs it as the separate
  `opencode-host` job.

## References

- [`opencode-bridge-contract.md`](opencode-bridge-contract.md) — the
  stdin/stdout contract, record shapes, version window and isolation property.
- [`../src/agent_runtime_platform/infrastructure/providers/opencode/host/README.md`](../src/agent_runtime_platform/infrastructure/providers/opencode/host/README.md)
  — the host package and its own checks.
- `.env.example` — the two OpenCode environment variables.
- Epic #67 and this delivery (#83); `docs/handover.md` records the decisions.
