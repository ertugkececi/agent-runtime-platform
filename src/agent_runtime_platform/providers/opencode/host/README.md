# OpenCode bridge host

The TypeScript package behind the `opencode` provider. It is the host side of
the wire contract in
[`docs/opencode-bridge-contract.md`](../../../../../docs/opencode-bridge-contract.md):
one JSON request on stdin, NDJSON records on stdout, one short-lived process per
call. The Python adapter (a later change in epic #67) spawns it; nothing else
talks to it.

The host runs the official `@opencode/sdk` in process — no listener, no daemon,
no hand-written REST wrapper. The SDK version is declared in
[`../manifest.toml`](../manifest.toml).

## Run it

```sh
bun install
printf '%s' '{
  "bridge_protocol": 1,
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

The process writes `hello` first, one `event` per tool lifecycle step, exactly
one final `result` or `error`, and exits zero only when it wrote a `result`.

## The tool policy

This provider refuses every tool grant, so the model must not see a tool at
all. The host passes OpenCode a permission policy that denies every action,
runs its session as a dedicated `bridge` agent that carries the same rules as
its last rules, installs a plugin that appends them to every other agent, and
hooks tool execution so a denied tool cannot run even if a definition leaked
into a catalog.

Before the turn starts, `verifyPolicy` re-reads the effective permissions from
the host and refuses to run unless every action is wholly denied. The check is
behavioural — it reads the rules the host will enforce, not the constant this
package ships.

## Configuration and isolation

Configuration reaches the host through its process environment; the request
never carries credentials. The host does not read the invoking user's
`~/.config/opencode`: it points OpenCode at a private config directory, and
when the environment does not already provide private `XDG_*` roots it makes
its own private root. The session runs in a private working directory, and the
model catalog is served from the bundled snapshot rather than a network fetch.

## Check it

```sh
bun install --frozen-lockfile   # the lock file must match package.json
bun run build                   # bundles the entry point into dist/
bun run typecheck
bun test                        # unit tests plus one test that starts the real embedded host
```

The `opencode-host` CI job runs these steps on their own, apart from the
Python jobs.

The test suite never reaches a model service: the tests that would call a model
use a fake host, and the embedded host test only re-checks the tool policy.
