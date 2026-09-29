# OpenCode bridge host

The TypeScript package behind the `opencode` provider. It is the host side of
the wire contract in
[`docs/opencode-bridge-contract.md`](../../../../../docs/opencode-bridge-contract.md):
one JSON request line on stdin, NDJSON records on stdout, one short-lived
process per call. The Python adapter spawns it; nothing else talks to it.

The host runs the official `@opencode/sdk` in process — no listener, no daemon,
no hand-written REST wrapper. The SDK version is declared in
[`../manifest.toml`](../manifest.toml).

## Run it

```sh
bun install
printf '%s' '{
  "bridge_protocol": 2,
  "model": "opencode/space-bunny-free",
  "instructions": "Answer in the user’s language.",
  "history": [{"role": "user", "content": "Summarize the queue module."}],
  "allow_handoff": false,
  "tool_ids": []
}' | bun run start
```

`model` is an OpenCode model reference, `provider/model` (an optional
`#variant` is accepted). Anything else is refused as a `request` error. The
contract's optional `reasoning_effort` is accepted; OpenCode selects effort
through model variants, so the adapter names it in the reference
(`provider/model#variant`) rather than in that field.

A call runs a turn, reads the model catalog, lists the integrations, or
connects one provider:

```sh
printf '%s' '{"bridge_protocol": 2, "operation": "models"}' | bun run start
printf '%s' '{"bridge_protocol": 2, "operation": "integrations"}' | bun run start
printf '%s' '{"bridge_protocol": 2, "operation": "connect", "integration": "openai"}' | bun run start
```

The catalog call boots the same private host, reads the bundled model snapshot
(no network), and answers with one `result` record whose `kind` is `models`.
Each entry carries the `provider/model` reference, a label, the default flag,
and the model's effort variants.

The connect call (through the adapter, which keeps its stdin open) writes one
`oauth` record with the sign-in details, then one `result` record whose `kind`
is `connected` once the credential is stored. A `code`-mode attempt reads one
further stdin line, `{"code": "..."}`, while it waits.

The process writes `hello` first, one `event` per tool lifecycle step, exactly
one final `result` or `error`, and exits zero only when it wrote a `result`.

## The tool policy

The runtime only grants administrator-approved, read-only MCP tools. The host
builds its permission policy from that grant list: one `allow` rule per
granted action, after the catch-all `deny`, because OpenCode's last matching
rule wins. Everything else — shell, file edits, web access, other MCP tools —
stays hidden from the model and cannot execute.

The host registers the granted servers from the request's `mcp_servers`
(credentials arrive through the process environment, by name), runs its
session as a dedicated `bridge` agent that carries the same rules as its last
rules, installs a plugin that appends them to every other agent, and hooks
tool execution so a tool outside the grant list cannot run even if a
definition leaked into a catalog.

Before the turn starts, `verifyPolicy` re-reads the effective permissions from
the host and refuses to run unless every dangerous action is wholly denied and
every granted tool is wholly allowed. The check is behavioural — it reads the
rules the host will enforce, not a constant this package ships.

## Configuration and isolation

Configuration reaches the host through its process environment; the request
never carries credential values. The adapter gives the host a private home per
call: `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME` point
into a directory the adapter owns (`0700`, removed when the call ends), and
the child runs with umask `077`, so files the host creates are `0600`. The one
exception is `XDG_DATA_HOME`: it points at the persistent, app-owned data root
that carries provider credentials across calls. A host started outside the
adapter makes its own private root for any root the environment left unset, so
the invoking user's `~/.config/opencode` is never read. The session runs in a
private working directory, and the model catalog is served from the bundled
snapshot rather than a network fetch.

The host writes only the severity of an SDK log entry to stderr; messages and
attributes may carry prompts, tool arguments, or results, and none of those are
logged. Library console output is redirected to stderr, because stdout is the
record stream.

## Check it

```sh
bun install --frozen-lockfile   # the lock file must match package.json
bun run build                   # bundles the entry point into dist/
bun run typecheck
bun test                        # unit tests plus tests that start the real embedded host
```

The `opencode-host` CI job runs these steps on their own, apart from the
Python jobs.

The test suite never reaches a model service: the tests that would call a model
use a fake host, the embedded-host tests never prompt, and the MCP grant test
starts a dependency-free fixture server (with no network) to prove the policy.
