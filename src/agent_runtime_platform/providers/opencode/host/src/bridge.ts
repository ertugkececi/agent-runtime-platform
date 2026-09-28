/**
 * The bridge: one request in, the record stream out.
 *
 * The order of the record stream is part of the contract. `hello` is always
 * first. A version mismatch or a tool grant fails before any host work starts.
 * A turn emits `event` records while it runs and ends with exactly one `result`
 * record. Every failure path emits exactly one `error` record and a non-zero
 * exit code.
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
  type ErrorRecord,
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
}

export type TurnHostFactory = (options: TurnHostOptions) => Promise<TurnHost>;

export interface BridgeDependencies {
  readonly createHost: TurnHostFactory;
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

  if (request.tool_ids.length > 0) {
    return fail(
      write,
      new BridgeError(
        "tool_refused",
        "tool_ids are not supported by this provider.",
      ),
    );
  }

  let host: TurnHost | undefined;
  try {
    host = await dependencies.createHost({
      model: request.model,
      system: buildSystemPrompt(request),
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
