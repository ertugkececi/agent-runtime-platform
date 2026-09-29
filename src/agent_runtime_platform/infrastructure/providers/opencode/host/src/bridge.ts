/**
 * The bridge: one request in, the record stream out.
 *
 * The order of the record stream is part of the contract. `hello` is always
 * first. A version mismatch fails before any host work starts. A turn emits
 * `event` records while it runs and ends with exactly one `result` record; a
 * catalog, integrations or connect request ends with the one `result` record
 * that answers it, and a connect request emits its `oauth` record first. Every
 * failure path emits exactly one `error` record and a non-zero exit code.
 */

import {
  BridgeError,
  hello,
  isCompatible,
  parseRequest,
  COMPATIBLE_MAX_EXCLUSIVE,
  COMPATIBLE_MIN,
  type BridgeRecord,
  type BridgeRequest,
  type CatalogModel,
  type ErrorRecord,
  type IntegrationDescriptor,
  type McpServerRequest,
  type OauthAttempt,
} from "./protocol";
import type { TraceEvent } from "./trace";
import { buildPromptText, buildSystemPrompt, interpretFinalText } from "./turn";

/** One SDK host, reduced to what a turn needs. */
export interface TurnHost {
  /** Re-read the effective tool policy; reject when it is not in force. */
  verifyPolicy(): Promise<void>;
  /** Run one model turn and return the final assistant text. */
  prompt(text: string, onTrace: (event: TraceEvent) => void): Promise<string | undefined>;
  close(): Promise<void>;
}

export interface TurnHostOptions {
  readonly model: string;
  readonly system: string;
  readonly mcpServers: readonly McpServerRequest[];
}

export type TurnHostFactory = (options: TurnHostOptions) => Promise<TurnHost>;

/** What a connection attempt asks its caller for, and what it reports. */
export interface ConnectionHooks {
  /** The sign-in details the human needs, the moment they exist. */
  onAttempt: (attempt: OauthAttempt) => void;
  /** The code the provider showed the human; only called in `code` mode. */
  waitForCode: (attempt: OauthAttempt) => Promise<string>;
}

export interface ConnectOptions {
  readonly integration: string;
  readonly method?: string;
  readonly label?: string;
}

export interface ConnectResult {
  readonly integration: string;
  readonly method: string;
}

export type ConnectRunner = (
  options: ConnectOptions,
  hooks: ConnectionHooks,
) => Promise<ConnectResult>;

export interface BridgeDependencies {
  readonly createHost: TurnHostFactory;
  /** Read the model catalog from one short-lived host. */
  readonly listModels: () => Promise<CatalogModel[]>;
  /** List the integrations the host offers. */
  readonly listIntegrations: () => Promise<IntegrationDescriptor[]>;
  /** Run one connection attempt to completion. */
  readonly connect: ConnectRunner;
  /** Read the next request line; only a `code`-mode connection calls this. */
  readonly readCode: () => Promise<string>;
  readonly write: (record: BridgeRecord) => void;
}

/** Run one bridge call and return the process exit code. */
export async function runBridge(input: string, dependencies: BridgeDependencies): Promise<number> {
  const { write } = dependencies;
  write(hello());

  let request: BridgeRequest;
  try {
    request = parseRequest(parseJson(input));
  } catch (error) {
    return fail(write, error);
  }

  if (!isCompatible(request.bridge_protocol)) {
    return fail(
      write,
      new BridgeError(
        "bridge_version",
        `The request bridge_protocol ${request.bridge_protocol} is outside the compatible ` +
          `window ${COMPATIBLE_MIN} <= v < ${COMPATIBLE_MAX_EXCLUSIVE}.`,
      ),
    );
  }

  if (request.operation === "models") {
    try {
      const models = await dependencies.listModels();
      write({ type: "result", kind: "models", models });
      return 0;
    } catch (error) {
      return fail(write, error);
    }
  }

  if (request.operation === "integrations") {
    try {
      const integrations = await dependencies.listIntegrations();
      write({ type: "result", kind: "integrations", integrations });
      return 0;
    } catch (error) {
      return fail(write, error);
    }
  }

  if (request.operation === "connect") {
    try {
      const connected = await dependencies.connect(
        {
          integration: request.integration,
          ...(request.method === undefined ? {} : { method: request.method }),
          ...(request.label === undefined ? {} : { label: request.label }),
        },
        {
          onAttempt: (attempt) => write({ type: "oauth", ...attempt }),
          waitForCode: async () => dependencies.readCode(),
        },
      );
      write({
        type: "result",
        kind: "connected",
        integration: connected.integration,
        method: connected.method,
      });
      return 0;
    } catch (error) {
      return fail(write, error);
    }
  }

  let host: TurnHost | undefined;
  try {
    host = await dependencies.createHost({
      model: request.model,
      system: buildSystemPrompt(request),
      mcpServers: request.mcp_servers ?? [],
    });
    // The permission is re-checked before the model sees any work.
    await host.verifyPolicy();
    const text = await host.prompt(buildPromptText(request), (event) => {
      write({ type: "event", ...event });
    });
    const result = interpretFinalText(text, request.allow_handoff);
    write({ type: "result", ...result });
    return 0;
  } catch (error) {
    return fail(write, error);
  } finally {
    if (host) {
      try {
        await host.close();
      } catch {
        // Closing is best effort; the process ends with the turn.
      }
    }
  }
}

/** Every failure becomes exactly one error record and a non-zero exit code. */
function fail(write: (record: BridgeRecord) => void, error: unknown): number {
  write(errorRecord(error));
  return 1;
}

/**
 * Only errors this host wrote itself describe themselves. Anything else is an
 * SDK or framework failure, and its text may carry credentials or internal
 * dumps, so it is collapsed into a label.
 */
function errorRecord(error: unknown): ErrorRecord {
  if (error instanceof BridgeError) {
    return { type: "error", kind: error.kind, message: error.message };
  }
  const label = errorLabel(error);
  return {
    type: "error",
    kind: "provider",
    message:
      label === undefined
        ? "The OpenCode host failed the model request."
        : `The OpenCode host failed the model request (${label}).`,
  };
}

function errorLabel(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  for (const key of ["_tag", "name"] as const) {
    const value = (error as Record<string, unknown>)[key];
    if (typeof value !== "string" || value === "Error") continue;
    if (/^[A-Za-z][A-Za-z0-9_.]{0,60}$/.test(value)) return value;
  }
  return undefined;
}

function parseJson(input: string): unknown {
  try {
    return JSON.parse(input);
  } catch {
    throw new BridgeError("request", "The request is not valid JSON.");
  }
}
