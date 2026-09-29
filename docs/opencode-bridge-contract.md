# OpenCode bridge contract

**Status:** the adapter and host implement this contract; it is at version 2
after the MCP and provider-connection delivery.

This document pins the one boundary between the Python adapter and the
TypeScript bridge host: the wire contract on stdin/stdout. Both sides
implement against this document and nothing else.

## Call model: per-call runner

Each call runs a short-lived TypeScript process. A call runs one model turn,
reads the model catalog, lists the provider integrations, or connects one
provider; all four use the same framing, version handshake, and error shape.

1. The adapter spawns the host with a private environment (see
   [Isolation](#isolation)) and writes exactly **one JSON request line** on
   stdin.
2. The host drives the call, writes **NDJSON records** to stdout, and exits.
3. The adapter reads stdout line by line, keeps the interesting records, and
   treats process exit as the end of the call.

There is no daemon, no socket, and no long-lived host for turns. The one
longer-lived case is a `connect` call: it answers with the sign-in details
while its process stays alive until the provider stores the credential.

Framing rules:

- stdout carries one JSON value per line, UTF-8, LF-terminated.
- Every record is a JSON object with a `type` field.
- The process exits `0` if and only if it emitted a `result` record.
- Every error path emits exactly one `error` record and exits non-zero.
- Excess bytes on stderr are diagnostic only; stderr is never parsed. An SDK
  log entry contributes only its severity (`opencode-host warn`); the host
  never writes prompts, tool arguments, tool results, or credentials to stderr,
  and it redirects any library console output to stderr so stdout stays a clean
  record stream.

## Versioning and compatibility

The request carries the protocol version the adapter speaks. The host
announces its own version as its first stdout record.

- Request field: `bridge_protocol`, integer. The adapter sends `2`.
- Host's first record: `{"type": "hello", "bridge_protocol": <host version>}`.
- A version `v` is compatible when `2 <= v < 3`.
- A version outside the window in either direction is an explicit error:
  the call fails with one `error` record of kind `bridge_version` and a
  non-zero exit, before any work starts.
- A host that does not emit `hello` (or emits a missing/non-integer
  `bridge_protocol`) counts as version `0` and fails the same way.
- Within the window, the adapter ignores record types it does not know. This
  is the only exception to "never silently adapt" in this contract: it exists
  so an additive host does not break an older adapter. Unknown *versions*
  are never ignored.

## Request (stdin)

One JSON object. Unknown fields are refused with a `request` error rather
than ignored.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `operation` | string | optional | `"turn"` (the default), `"models"`, `"integrations"`, or `"connect"`. Each non-turn request carries `bridge_protocol`, `operation`, and only its own fields. |
| `bridge_protocol` | int | yes | Protocol version the adapter speaks. |
| `model` | string | turn | Model identifier to run. |
| `instructions` | string | turn | Developer instructions; may carry the handoff contract. |
| `history` | array | turn | Chronological messages, each `{"role": "user"\|"assistant", "content": string}`. |
| `allow_handoff` | bool | turn | Whether the result may be a handoff instead of a reply. |
| `tool_ids` | array | turn | Granted tool ids, `server/tool`. Empty unless `mcp_servers` is present. |
| `mcp_servers` | array | with grants | The administrator-approved servers behind `tool_ids`; see below. |
| `remote_capabilities` | array | optional | Only meaningful when `allow_handoff` is true. |
| `reasoning_effort` | string | optional | Accepted and type-validated by the host, but not applied: it selects no effort by itself. The effective effort travels as a `#variant` suffix on `model`. The adapter never sends this field; it maps the agent's `model_reasoning_effort` to that suffix when the model name carries none. See [`opencode-provider.md`](opencode-provider.md). |
| `integration` | string | connect | Integration id, for example `openai`. |
| `method` | string | optional | Sign-in method id; when omitted the host prefers the headless method. |
| `label` | string | optional | Account label the credential is stored under. |

Example turn:

```json
{
  "bridge_protocol": 2,
  "model": "opencode/big-model#high",
  "instructions": "Follow the agent instructions. Answer in the user's language.",
  "history": [
    {"role": "user", "content": "Summarize the queue module."}
  ],
  "allow_handoff": false,
  "tool_ids": ["fixture/lookup"],
  "mcp_servers": [
    {
      "name": "fixture",
      "command": "uvx",
      "args": ["example-readonly-mcp"],
      "cwd": "/srv/fixture",
      "env_vars": ["FIXTURE_TOKEN"],
      "tools": ["lookup"]
    }
  ]
}
```

### `mcp_servers` — the approved tool grants

- Only the granted tools of each server travel; arguments and environment
  **names** travel, environment **values** never do. The host resolves the
  names from its own process environment, whose roots the adapter owns.
- The host registers each server as a local MCP server, allows exactly the
  granted tools' permission actions, and denies every other action, so the
  model's catalog contains the grants and nothing else.
- A `turn` with grants but no `mcp_servers`, or servers without a grant, is a
  `request` error. An empty `tools` list is a `request` error.
- A tool the administrator no longer approves never reaches the host: the
  adapter refuses the turn first.

### `models` — the catalog

```json
{"bridge_protocol": 2, "operation": "models"}
```

- A `models` request carries no turn field; any other field on it is a
  `request` error, because unknown fields are refused rather than ignored.
- The host reads the bundled model catalog, never the network, and never
  prompts. A provider without credentials simply contributes no models, so an
  empty catalog is a valid answer.
- The single `result` record is `{"type": "result", "kind": "models",
  "models": […]}`; each entry is described under
  [Records](#records-stdout-ndjson).

### `integrations` — the sign-in catalog

```json
{"bridge_protocol": 2, "operation": "integrations"}
```

- The host answers with the integrations that offer at least one sign-in
  method: `{"type": "result", "kind": "integrations", "integrations": […]}`.
- Each integration is `{id, name, connected, methods}`; each method is
  `{id, type, label}`. Nothing else about the integration is sent.

### `connect` — one provider sign-in

```json
{"bridge_protocol": 2, "operation": "connect", "integration": "openai", "method": "chatgpt-headless", "label": "work"}
```

- The host starts the OAuth attempt, writes one `oauth` record with the
  sign-in details, and waits.
- `mode: "auto"`: the provider completes the exchange on its own; the host
  polls until the attempt is complete, failed or expired.
- `mode: "code"`: the human submits a code; the adapter writes **one further
  stdin line**, `{"code": "<code>"}`, and the host completes the attempt.
- On success the host writes `{"type": "result", "kind": "connected",
  "integration": …, "method": …}` and exits. The credential is stored in the
  persistent data root (see [Isolation](#isolation)).

## Records (stdout NDJSON)

Each record after `hello` is one of the following. Fields named but not
listed for a record type are not sent.

### `event` — tool trace

```json
{"type": "event", "server": "files", "tool": "search", "status": "running", "phase": "running"}
```

- `server`, `tool`, `status`, `phase` are strings.
- An event record **never** carries tool arguments, tool results, prompts, or
  model output. This is the whole point of the record: the runtime shows a
  trace, not a transcript.
- The set of allowed status/phase values is not a contract surface; both
  sides treat them as opaque display text.

### `oauth` — the sign-in details

```json
{"type": "oauth", "attempt_id": "attempt-1", "url": "https://auth.example/device", "instructions": "Enter code: ABCD-EFGH", "mode": "auto"}
```

- Written once, before the completion record of a `connect` call.
- `attempt_id` identifies the attempt inside the host; `url` and
  `instructions` are shown to the human; `mode` is `auto` or `code`.
- Nothing about the account, the token, or the provider's response is sent.

### `result` — the answer

```json
{"type": "result", "kind": "reply", "content": "The queue lives in src/agent_runtime_platform/queue.py."}
```

```json
{"type": "result", "kind": "handoff", "capability": "research", "task": "Find the SQLite schema for queue_jobs."}
```

```json
{"type": "result", "kind": "models", "models": [{"id": "opencode/big-model", "label": "Big Model", "is_default": true, "default_effort": "", "efforts": ["low", "medium", "high", "xhigh", "max"]}]}
```

```json
{"type": "result", "kind": "integrations", "integrations": [{"id": "openai", "name": "OpenAI", "connected": false, "methods": [{"id": "chatgpt-headless", "type": "oauth", "label": "ChatGPT Pro/Plus (headless)"}]}]}
```

```json
{"type": "result", "kind": "connected", "integration": "openai", "method": "chatgpt-headless"}
```

- A call produced at most one `result` record, and it is the last record.
- `kind: "reply"` yields the assistant's final text.
- `kind: "handoff"` yields a runtime `HandoffRequest` with `capability` and
  `task`; `handoff` is only allowed when the request set `allow_handoff`.
- `reply` content is the model's final answer only; no concatenated stream.
- `kind: "models"` answers a `models` request: the catalog in the shape the
  HTTP model catalogs share. `id` is the `provider/model` reference a turn
  sends as `model`; `label` is display text; `is_default` marks the host's
  default; `efforts` are the model's variant ids; `default_effort` is empty
  when the model has no default variant.
- `kind: "integrations"` answers an `integrations` request.
- `kind: "connected"` answers a `connect` request after the credential was
  stored.

### `error` — everything else fails here

```json
{"type": "error", "kind": "request", "message": "A turn that grants tool_ids must carry their mcp_servers."}
```

```json
{"type": "error", "kind": "bridge_version", "message": "The request bridge_protocol 1 is outside the compatible window 2 <= v < 3."}
```

| Kind | Raised when |
| --- | --- |
| `bridge_version` | Version handshake fails in either direction (see above). |
| `request` | The request object is invalid (missing field, wrong type, unknown field, grants without servers). |
| `provider` | The model or connection interaction failed (SDK or provider error). |
| `timeout` | Not emitted by the host; see [Timeouts](#timeouts). |

`message` is a user-safe, non-empty string. It never contains credentials,
secrets, or raw SDK dumps.

## The six provider expectations

- **Handoff protocol.** `allow_handoff: false` forces a `reply`. With
  `allow_handoff: true` the host may return a `handoff` carrying exactly one
  `capability` and one `task`, matching the runtime's `HandoffRequest`.
- **Tool grants.** A non-empty `tool_ids` must travel with its `mcp_servers`.
  The host allows exactly the granted tool actions and denies every other
  action; a tool outside the grant list cannot execute even if a definition
  leaked into a catalog. There is no silent adaptation.
- **Event trace.** Tool activity surfaces only through `event` records with
  `{server, tool, status, phase}`.
- **Secrets.** Configuration reaches the host through its process
  environment; the request never carries credential values, and no record
  ever echoes them.
- **Isolation.** The host must not inherit the user's `~/.config/opencode`.
  See the next section.
- **Errors.** Every failure collapses into one `error` record. Version
  mismatch in particular is explicit, never a quiet follower of unknown
  behaviour.

## Isolation

The adapter gives each call a private home directory and the host never reads
the invoking user's OpenCode configuration. The concrete mechanism:

- The adapter creates one private home per call (`0700`), passes `HOME` and
  `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME` pointing inside it,
  and removes the directory when the call ends. The invoking user's roots are
  replaced, never forwarded.
- `XDG_DATA_HOME` is the one deliberate exception: it points at the
  persistent, app-owned data root (`AGENT_RUNTIME_OPENCODE_HOME`, mode
  `0700`), because that is where OpenCode stores the credential database a
  sign-in must survive in. The invoking user's own data root is still never
  used.
- The child runs with umask `077`, so every file the host creates is `0600`
  and every directory is `0700`.
- The host itself still fills in unset roots with a private directory, so a
  host started outside the adapter never falls back to `~/.config/opencode`;
  an environment that already provides private roots wins.

This is the property, not a specific variable name: whatever resolves the
user's configuration must not be reachable from the host process.

## Timeouts

One wall-clock budget per call, enforced by the adapter from provider
configuration (default and maximum are provider settings, not contract
fields). On expiry the adapter kills the process and fails the call with a
`timeout`-style error mapped to a runtime `ProviderError` ("The OpenCode
model request timed out."). The same budget bounds a `connect` call while it
waits for its human. The host must not run unbounded; there is no
client-driven keep-alive.

## Mapping into the runtime

| Bridge record | Runtime outcome |
| --- | --- |
| `result` / `reply` | `ModelOutput` (string) |
| `result` / `handoff` | `HandoffRequest(capability, task)` |
| `result` / `models` | the HTTP model catalog (`GET /opencode/models`) |
| `result` / `integrations` | the HTTP integration list (`GET /opencode/integrations`) |
| `oauth` + `result` / `connected` | a stored credential; the connection API reports it |
| `error` (`request`) | explicit `ProviderError`, surfaced as `422` |
| `error` (`provider`, `bridge_version`) | `ProviderError`, surfaced as `500`-class unless a mapping says otherwise |
| kill on timeout | `ProviderError` ("…timed out.") |

The event records feed the runtime `tool_event_callback` shape
`{server, tool, status}` plus `phase`.

## Guarding this contract

The pytest suite pins side-by-side examples of every record, a version
handshake table (compatible and refused), a turn with MCP grants, a `models`
catalog answer, an `integrations` answer, both `connect` modes, and a "result
is last and unique" assertion. This document and its examples are the
authority for those tests. The bridge is not part of the generated HTTP
contract (`contracts/openapi.json`); that drift rule is untouched.
