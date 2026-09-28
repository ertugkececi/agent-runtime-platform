# OpenCode bridge contract

**Status:** draft — first delivery of the OpenCode provider epic (#67).

This document pins the one boundary between the Python adapter and the
TypeScript bridge host: the wire contract on stdin/stdout. Both sides
implement against this document and nothing else. It records only the
decisions that were already taken for the epic; see `docs/handover.md` for
the surrounding record and issue #77 for the acceptance criteria behind this
text.

## Call model: per-call runner

Each call runs a short-lived TypeScript process. A call either runs one model
turn or reads the model catalog; both use the same framing, version handshake,
and error shape.

1. The adapter spawns the host with a private environment (see
   [Isolation](#isolation)) and writes exactly **one JSON request** object on
   stdin.
2. The host drives one model interaction (`OpenCode.create()` then
   `sessions.prompt()`), or reads the model catalog, writes **NDJSON records**
   to stdout, and exits.
3. The adapter reads stdout line by line, keeps the interesting records, and
   treats process exit as the end of the call.

There is no daemon, no socket, no shared state between calls, and no
long-lived host. This mirrors the Codex provider's `tempfile` plus
`with Codex(...)` pattern. A persistent host is deferred until a measurement
asks for it.

Framing rules:

- stdout carries one JSON value per line, UTF-8, LF-terminated.
- Every record is a JSON object with a `type` field.
- The process exits `0` if and only if it emitted a `result` record.
- Every error path emits exactly one `error` record and exits non-zero.
- Excess bytes on stderr are diagnostic only; stderr is never parsed.

## Versioning and compatibility

The request carries the protocol version the adapter speaks. The host
announces its own version as its first stdout record.

- Request field: `bridge_protocol`, integer. The adapter sends `1`.
- Host's first record: `{"type": "hello", "bridge_protocol": <host version>}`.
- A version `v` is compatible when `1 <= v < 2`.
- A version outside the window in either direction is an explicit error:
  the turn fails with one `error` record of kind `bridge_version` and a
  non-zero exit, before any model work starts.
- A host that does not emit `hello` (or emits a missing/non-integer
  `bridge_protocol`) counts as version `0` and fails the same way.
- Within the window, the adapter ignores record types it does not know. This
  is the only exception to "never silently adapt" in this contract: it exists
  so an additive 1.x host does not break an older adapter. Unknown *versions*
  are never ignored.
- The `models` operation is additive inside the window: it does not change the
  version. An adapter that asks an older host for models gets one explicit
  `request` error, because the host refuses the unknown `operation` field.

## Request (stdin)

One JSON object. Unknown fields are refused with a `request` error rather
than ignored.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `operation` | string | optional | `"turn"` (the default) or `"models"`. A `models` request carries `bridge_protocol` and `operation` and nothing else. |
| `bridge_protocol` | int | yes | Protocol version the adapter speaks. |
| `model` | string | yes | Model identifier to run. |
| `instructions` | string | yes | Developer instructions; may carry the tool restriction and the handoff contract. |
| `history` | array | yes | Chronological messages, each `{"role": "user"\|"assistant", "content": string}`. |
| `allow_handoff` | bool | yes | Whether the result may be a handoff instead of a reply. |
| `tool_ids` | array | yes | Tool ids for this turn; **must be empty** — see [Tool refusal](#tool-refusal). |
| `remote_capabilities` | array | optional | Only meaningful when `allow_handoff` is true. |
| `reasoning_effort` | string | optional | Effort level where the model supports one. |

Example:

```json
{
  "bridge_protocol": 1,
  "model": "opencode-compatible",
  "instructions": "Follow the agent instructions. Do not use tools. Answer in the user's language.",
  "history": [
    {"role": "user", "content": "Summarize the queue module."}
  ],
  "allow_handoff": false,
  "tool_ids": []
}
```

### `models` — the catalog

```json
{"bridge_protocol": 1, "operation": "models"}
```

- A `models` request carries no turn field; any other field on it is a
  `request` error, because unknown fields are refused rather than ignored.
- The host reads the bundled model catalog, never the network, and never
  prompts. A provider without credentials simply contributes no models, so an
  empty catalog is a valid answer.
- The single `result` record is `{"type": "result", "kind": "models",
  "models": […]}`; each entry is described under [Records](#records-stdout-ndjson).

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

### `result` — the answer

```json
{"type": "result", "kind": "reply", "content": "The queue lives in src/agent_runtime_platform/queue.py."}
```

```json
{"type": "result", "kind": "handoff", "capability": "research", "task": "Find the SQLite schema for queue_jobs."}
```

```json
{"type": "result", "kind": "models", "models": [{"id": "opencode/space-bunny-free", "label": "Space Bunny Free", "is_default": true, "default_effort": "", "efforts": ["low", "medium", "high", "xhigh", "max"]}]}
```

- A turn produced at most one `result` record, and it is the last record.
- `kind: "reply"` yields the assistant's final text.
- `kind: "handoff"` yields a runtime `HandoffRequest` with `capability` and
  `task`; `handoff` is only allowed when the request set `allow_handoff`.
- `reply` content is the model's final answer only; no concatenated stream.
- `kind: "models"` answers a `models` request: the catalog in the shape the
  HTTP model catalogs share. `id` is the `provider/model` reference a turn
  sends as `model`; `label` is display text; `is_default` marks the host's
  default; `efforts` are the model's variant ids; `default_effort` is empty
  when the model has no default variant.

### `error` — everything else fails here

```json
{"type": "error", "kind": "tool_refused", "message": "tool_ids are not supported by this provider."}
```

```json
{"type": "error", "kind": "bridge_version", "message": "host bridge_protocol 2 is outside the compatible window 1 <= v < 2."}
```

| Kind | Raised when |
| --- | --- |
| `bridge_version` | Version handshake fails in either direction (see above). |
| `tool_refused` | `tool_ids` is non-empty. |
| `request` | The request object is invalid (missing field, wrong type, unknown field). |
| `provider` | The model interaction failed (SDK or provider error). |
| `timeout` | Not emitted by the host; see [Timeouts](#timeouts). |

`message` is a user-safe, non-empty string. It never contains credentials,
secrets, or raw SDK dumps.

## The six provider expectations

- **Handoff protocol.** `allow_handoff: false` forces a `reply`. With
  `allow_handoff: true` the host may return a `handoff` carrying exactly one
  `capability` and one `task`, matching the runtime's `HandoffRequest`.
- **Tool refusal.** A non-empty `tool_ids` is refused explicitly, exactly as
  the OpenAI provider refuses unsupported tools: one `tool_refused` error and
  no call. There is no silent adaptation and no MCP mapping in this slice.
- **Event trace.** Tool activity surfaces only through `event` records with
  `{server, tool, status, phase}`.
- **Secrets.** Configuration reaches the host through its process
  environment; the request never carries credentials or tokens, and no record
  ever echoes them.
- **Isolation.** The host must not inherit the user's `~/.config/opencode`.
  See the next section.
- **Errors.** Every failure collapses into one `error` record. Version
  mismatch in particular is explicit, never a quiet follower of unknown
  behaviour.

## Isolation

The adapter passes the host a private home directory through the
environment and it uses `0700` for directories and `0600` for files, the way
`codex_home.py` does for Codex. The bridge host is run with that private home
and never the invoking user's OpenCode configuration. On macOS the adapter
sets the `HOME` (or the SDK's config-root equivalent) for the child process
to that directory. The contract here is the property, not a specific
environment variable name; #82 pins the concrete mechanism.

## Timeouts

One wall-clock budget per call, enforced by the adapter from provider
configuration (default and maximum are provider settings, not contract
fields). On expiry the adapter kills the process and fails the turn with a
`timeout`-style error mapped to a runtime `ProviderError` ("The OpenCode
model request timed out."). The host must not run unbounded; there is no
client-driven keep-alive.

## Mapping into the runtime

| Bridge record | Runtime outcome |
| --- | --- |
| `result` / `reply` | `ModelOutput` (string) |
| `result` / `handoff` | `HandoffRequest(capability, task)` |
| `result` / `models` | the HTTP model catalog (`GET /opencode/models`) |
| `error` (`tool_refused`, `request`) | explicit `ProviderError`, surfaced as `422` |
| `error` (`provider`, `bridge_version`) | `ProviderError`, surfaced as `500`-class unless a mapping says otherwise |
| kill on timeout | `ProviderError` ("…timed out.") |

The event records feed the existing `tool_event_callback` shape
`{server, tool, status}` plus `phase`, which the Codex provider already
delivers.

## Guarding this contract

The pytest suite pins side-by-side examples of every record, a version
handshake table (compatible and refused), a `tool_refused` turn, a `models`
catalog answer, and a "result is last and unique" assertion. This document and
its examples are the authority for those tests. The bridge is not part of the
generated HTTP contract (`contracts/openapi.json`); that drift rule is
untouched.

## References

- Issue #77 (this delivery) and epic #67.
- `docs/handover.md` — decisions taken for the epic and the delivery order.
- `src/agent_runtime_platform/providers/codex/provider.py` — the pattern this
  bridge mirrors.
- `src/agent_runtime_platform/codex_home.py` — the isolation pattern.