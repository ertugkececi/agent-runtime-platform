# Outgoing A2A 1.0 delegation

The runtime supports one outgoing A2A delegation within its existing human chat handoff. It uses the A2A HTTP+JSON 1.0 binding and a server administrator controlled catalog. It does not expose an incoming A2A server.

## Configure trusted targets

Set `AGENT_RUNTIME_A2A_TARGETS` in the application server environment to a JSON array. Example for a private HTTPS service:

```json
[
  {
    "id": "research-prod",
    "url": "https://research-agent.example.ts.net",
    "capabilities": ["research"],
    "allow_private": true,
    "token_env": "RESEARCH_A2A_TOKEN",
    "security_scheme": "bearerAuth",
    "request_timeout_seconds": 10,
    "max_wait_seconds": 60,
    "poll_interval_seconds": 1
  }
]
```

The referenced token value belongs in the server's environment, never in this JSON or an agent record. `security_scheme` names an Agent Card security scheme whose `httpAuthSecurityScheme.scheme` is `Bearer`. Cards without `securitySchemes` may be used without a token. Other authentication methods are rejected.

Use an HTTPS `url` in production. For a Tailscale/private target or local test fixture, set `"allow_private": true`; this permits non-global addresses and HTTP only when every resolved address is non-global. Redirects are disabled. The Agent Card URL defaults to the origin's `/.well-known/agent-card.json`; an administrator may set `card_url` explicitly on the same origin. The selected `supportedInterfaces` entry must advertise `protocolBinding: "HTTP+JSON"` and `protocolVersion: "1.0"`, use that exact origin (including port), and list the requested capability in a skill id or tag.

`GET /a2a/targets` returns only `{id, kind, capabilities}`. It never returns endpoint URLs or credentials. The parent model is given the available remote capabilities, but not their addresses or tokens. A remote target is matched only by an exact capability supplied through the existing handoff. Duplicate local/remote matches are rejected rather than selected implicitly.

## Wire behavior

The client sends a plain text user `Message` to `POST {interface-url}/message:send` with `Content-Type: application/a2a+json`, `A2A-Version: 1.0`, a persisted stable `messageId`, `role: "ROLE_USER"`, and `parts: [{"text": ...}]`. A direct `message` response is returned as untrusted text. A `task` response persists its remote id before polling `GET {interface-url}/tasks/{id}`. Working state updates, terminal status, errors, remote ids, and result are attached to the local delegated task and human-chat run events. Only text parts from messages/artifacts are consumed; binary and structured parts are ignored.

Requests have finite timeouts, bounded polling, no redirects, and a 1 MiB response cap. Agent Card interface URLs and task polling use the administrator-approved origin. URLs supplied by model output are never used.

## Retry semantics

The local child task, target id, target fingerprint, and stable A2A `messageId` are committed before the remote send. If sending may have reached the server but the runtime has not durably recorded a remote task id or direct result, the local status becomes `submission_unknown`; retries do not POST again. This avoids duplicate remote work but may require an operator to reconcile an uncertain send with the remote service. If a remote task id is known, retries resume with `Get Task` polling. A completed direct or task result is reused. Changing the endpoint, credentials source, or capability mapping under an existing target id invalidates its fingerprint; use a new target id for a changed endpoint.

Remote text is untrusted content. It is passed to the parent model as data with an explicit instruction not to treat it as instructions. Tokens, authorization headers, raw exceptions, and configured URLs are excluded from task snapshots and events.

## Limits

This slice does not support streaming, push notifications, multi-turn A2A contexts, OAuth, API keys, mTLS, file transfer, or arbitrary media. It only accepts plain text and a single remote target per delegated child. Incoming A2A and multi-user authentication need a separate security design.
