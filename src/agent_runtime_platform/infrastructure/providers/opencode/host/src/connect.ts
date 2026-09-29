/**
 * Connecting one provider integration with the embedded host.
 *
 * The flow is the SDK's: begin an OAuth attempt, hand the human the URL and
 * instructions, then wait until the server reports the attempt complete. A
 * `code`-mode attempt additionally needs the code the provider shows the human,
 * which the bridge reads from stdin. The resulting credential is stored in the
 * host's data root, which the adapter keeps across calls.
 */

import { BridgeError, type IntegrationDescriptor, type IntegrationMethodDescriptor, type OauthAttempt } from "./protocol";
import { bootPrivateHost, type PrivateHost } from "./session";
import type { ConnectionHooks, ConnectOptions, ConnectResult } from "./bridge";

const CONNECT_SYSTEM = "The bridge host agent. It connects a provider integration and runs no turn.";
const STATUS_POLL_MS = 1000;

/** One SDK method, reduced to what choosing a sign-in method reads. */
interface MethodLike {
  readonly id?: unknown;
  readonly type?: unknown;
  readonly label?: unknown;
}

interface IntegrationLike {
  readonly id: unknown;
  readonly name?: unknown;
  readonly methods: readonly MethodLike[];
  readonly connections: readonly unknown[];
}

/** List the integrations the host offers, and nothing else. */
export async function listIntegrations(): Promise<IntegrationDescriptor[]> {
  const host = await bootPrivateHost(CONNECT_SYSTEM);
  try {
    const result = await host.opencode.integration.list();
    return (result.data as unknown as readonly IntegrationLike[]).map(descriptor);
  } finally {
    // A failed close must not replace the answer the call produced.
    await host.close().catch(() => {});
  }
}

/** Run one connection attempt; resolves only when the credential is stored. */
export async function startConnect(
  options: ConnectOptions,
  hooks: ConnectionHooks,
): Promise<ConnectResult> {
  const host = await bootPrivateHost(CONNECT_SYSTEM);
  try {
    const integrations = (await host.opencode.integration.list()).data as unknown as readonly IntegrationLike[];
    const integration = integrations.find((candidate) => String(candidate.id) === options.integration);
    if (integration === undefined) {
      throw new BridgeError("request", `The integration '${options.integration}' is not available.`);
    }
    const integrationID = String(integration.id);
    const method = pickMethod(integration.methods, options.method);

    const result = await host.opencode.integration.oauth.connect({
      integrationID,
      methodID: method.id,
      ...(options.label === undefined ? {} : { label: options.label }),
    });
    const attempt = result.data;
    const details: OauthAttempt = {
      attempt_id: String(attempt.attemptID),
      url: attempt.url,
      instructions: attempt.instructions,
      mode: attempt.mode,
    };
    hooks.onAttempt(details);
    if (attempt.mode === "code") {
      const code = await hooks.waitForCode(details);
      await host.opencode.integration.oauth.complete({
        integrationID,
        attemptID: attempt.attemptID,
        code,
      });
    }
    await waitForCompletion(host, integrationID, String(attempt.attemptID));
    return { integration: integrationID, method: method.id };
  } finally {
    // A failed close must not replace the answer the call produced.
    await host.close().catch(() => {});
  }
}

/** Poll the attempt until the server stores the credential or reports failure. */
async function waitForCompletion(
  host: PrivateHost,
  integrationID: string,
  attemptID: string,
): Promise<void> {
  for (;;) {
    const status = (
      await host.opencode.integration.oauth.status({ integrationID, attemptID })
    ).data;
    if (status.status === "complete") return;
    if (status.status === "failed") {
      throw new BridgeError("provider", "The provider connection failed. Try again.");
    }
    if (status.status === "expired") {
      throw new BridgeError("provider", "The provider connection expired before it completed. Try again.");
    }
    await new Promise((resolve) => setTimeout(resolve, STATUS_POLL_MS));
  }
}

/**
 * Pick the sign-in method: an explicit id wins; otherwise the headless method
 * is preferred so a server without a reachable browser callback can sign in.
 */
function pickMethod(
  methods: readonly MethodLike[],
  requested: string | undefined,
): { id: string; label: string } {
  const oauth = methods.filter(
    (method): method is MethodLike & { id: string } =>
      method.type === "oauth" && typeof method.id === "string",
  );
  if (requested !== undefined) {
    const match = oauth.find((method) => method.id === requested);
    if (match === undefined) {
      throw new BridgeError(
        "request",
        `The requested sign-in method '${requested}' is not available.`,
      );
    }
    return { id: match.id, label: labelOf(match) };
  }
  const chosen =
    oauth.find((method) => method.id.includes("headless") || labelOf(method).toLowerCase().includes("headless")) ??
    oauth[0];
  if (chosen === undefined) {
    throw new BridgeError("request", "The integration offers no OAuth sign-in method.");
  }
  return { id: chosen.id, label: labelOf(chosen) };
}

function labelOf(method: MethodLike): string {
  return typeof method.label === "string" && method.label ? method.label : String(method.id);
}

function descriptor(integration: IntegrationLike): IntegrationDescriptor {
  const methods: IntegrationMethodDescriptor[] = [];
  for (const method of integration.methods) {
    if (typeof method.id !== "string") continue;
    methods.push({
      id: method.id,
      type: typeof method.type === "string" ? method.type : "unknown",
      label: labelOf(method),
    });
  }
  return {
    id: String(integration.id),
    name: typeof integration.name === "string" ? integration.name : String(integration.id),
    methods,
    connected: integration.connections.length > 0,
  };
}
